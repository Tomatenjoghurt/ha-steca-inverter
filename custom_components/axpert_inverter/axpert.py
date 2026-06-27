import time
import threading
import logging
import usb.core
import usb.util

_LOGGER = logging.getLogger(__name__)


def _find_device_by_port(port_path: str, vid: int, pid: int):
    """
    Find a USB device by its physical port path (e.g. '1-1.3') instead of
    just VID:PID, so identical devices on different ports can be distinguished.

    port_path format: '<bus>-<port>[.<port>...]'
    Examples: '1-1', '1-1.1', '1-1.3', '1-2.4.1'
    """
    try:
        parts = port_path.strip().split('-')
        bus = int(parts[0])
        port_numbers = list(map(int, parts[1].split('.')))
    except (IndexError, ValueError) as e:
        raise ValueError(
            f"Invalid port_path '{port_path}'. Expected format: '<bus>-<port>[.<subport>...]' "
            f"e.g. '1-1.3'. Error: {e}"
        )

    def match(dev):
        try:
            return (
                dev.idVendor == vid
                and dev.idProduct == pid
                and dev.bus == bus
                and list(dev.port_numbers) == port_numbers
            )
        except Exception:
            return False

    device = usb.core.find(custom_match=match)
    if device is None:
        raise ValueError(
            f"Device {hex(vid)}:{hex(pid)} not found on port '{port_path}' "
            f"(bus={bus}, ports={port_numbers}). "
            f"Make sure the device is plugged into that exact USB port."
        )
    return device


class USBConnection:
    """Helper to handle USB connection, resolved by physical port path."""

    def __init__(self, port_path: str, vid=0x0665, pid=0x5161, timeout=5000):
        self.port_path = port_path
        self.vid = vid
        self.pid = pid
        self.timeout = timeout
        self.dev = None
        self.ep_in = None
        self.ep_out = None

    def __enter__(self):
        # Resolve the device by physical USB port path, not just VID:PID.
        # This is the key fix: previously usb.core.find(idVendor=..., idProduct=...)
        # was used, which returns an arbitrary device when multiple identical
        # devices are connected. Now we match by bus + port numbers instead.
        self.dev = _find_device_by_port(self.port_path, self.vid, self.pid)

        # Helper to detach kernel driver for interface 0 (the main HID interface)
        if self.dev.is_kernel_driver_active(0):
            try:
                self.dev.detach_kernel_driver(0)
                _LOGGER.debug("Detached kernel driver from interface 0")
            except usb.core.USBError as e:
                _LOGGER.warning(f"Could not detach kernel driver: {e}")

        # Set configuration
        try:
            self.dev.set_configuration()
        except usb.core.USBError as e:
            if e.errno == 16:  # Resource busy
                _LOGGER.debug("Device busy during set_configuration, assuming already configured.")
            else:
                _LOGGER.warning(f"Could not set configuration: {e}")

        # Explicitly claim interface 0
        try:
            usb.util.claim_interface(self.dev, 0)
        except usb.core.USBError as e:
            _LOGGER.error(f"Could not claim interface 0: {e}")
            raise e

        # Iterate over all configurations/interfaces/endpoints to find the first valid pair
        cfg = self.dev.get_active_configuration()
        for intf in cfg:
            for ep in intf:
                if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_OUT:
                    if self.ep_out is None:
                        self.ep_out = ep
                elif usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN:
                    if self.ep_in is None:
                        self.ep_in = ep
            if self.ep_out and self.ep_in:
                break

        if not self.ep_in:
            _LOGGER.error(f"Could not find IN endpoint. Configuration: {cfg}")
            raise ValueError("Could not find IN endpoint")

        if not self.ep_out:
            _LOGGER.debug("No OUT endpoint found. Will use Control Transfer (SET_REPORT) for writing.")

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            usb.util.release_interface(self.dev, 0)
        except Exception:
            pass
        if self.dev is not None:
            usb.util.dispose_resources(self.dev)

    def write(self, data: bytes):
        if self.ep_out:
            # Use Interrupt OUT
            chunk_size = 8
            offset = 0
            while offset < len(data):
                chunk = data[offset:offset + chunk_size]
                self.ep_out.write(chunk, self.timeout)
                offset += chunk_size
                time.sleep(0.01)
        else:
            # Use Control Transfer (HID SET_REPORT)
            # bmRequestType: 0x21 (Host to Device | Class | Interface)
            # bRequest: 0x09 (SET_REPORT)
            # wValue: (0x02 << 8) | 0x00 (Output Report, ID 0)
            # wIndex: 0 (Interface 0)
            chunk_size = 8
            offset = 0
            while offset < len(data):
                chunk = data[offset:offset + chunk_size]
                try:
                    self.dev.ctrl_transfer(0x21, 0x09, 0x200, 0, chunk, self.timeout)
                except usb.core.USBError as e:
                    _LOGGER.error(f"Control transfer failed: {e}")
                    raise e
                offset += chunk_size
                time.sleep(0.01)

    def read_until(self, terminator=b'\r') -> bytes:
        if not self.ep_in:
            return b""
        res = b""
        start = time.time()
        timeout_sec = self.timeout / 1000.0
        while (time.time() - start) < timeout_sec:
            try:
                data = self.ep_in.read(8, 200)
                res += bytes(data)
                if terminator in res:
                    break
            except usb.core.USBError as e:
                if e.errno == 110:  # Timeout
                    continue
                continue
        return res

    def reset_input_buffer(self):
        pass

    def reset_output_buffer(self):
        pass


class AxpertInverter:
    """Class to communicate with the Axpert Inverter via HID."""

    def __init__(self, device_path: str):
        """
        Initialize the inverter interface.

        device_path must be a USB port path in the format '<bus>-<port>[.<subport>...]',
        e.g. '1-1.1', '1-1.3', '1-1.4'.

        This is the physical USB port address as reported by the kernel, which remains
        stable regardless of enumeration order. Obtain it by running:
            ls /sys/bus/usb/devices/
        or:
            udevadm info -a -n /dev/hidraw0 | grep KERNELS
        while each device is plugged into its designated port.
        """
        self._device_path = device_path
        self._lock = threading.Lock()
        self._last_command_time = 0

    def _get_crc(self, cmd: str | bytes) -> bytes:
        """Calculate CRC16-XMODEM."""
        crc = 0
        if isinstance(cmd, str):
            da = bytearray(cmd, 'utf8')
        else:
            da = bytearray(cmd)
        for byte in da:
            crc ^= byte << 8
            for _ in range(8):
                if (crc & 0x8000):
                    crc = ((crc << 1) ^ 0x1021) & 0xFFFF
                else:
                    crc = (crc << 1) & 0xFFFF
        low = crc & 0xFF
        high = (crc >> 8) & 0xFF
        # Fix for control characters in CRC (from Voltronic protocol)
        if low in (0x28, 0x0d, 0x0a):
            low += 1
        if high in (0x28, 0x0d, 0x0a):
            high += 1
        return bytes([high, low])

    def send_command(self, command: str) -> str:
        """Send a command to the inverter and return the response."""
        with self._lock:
            # Ensure at least 500ms between commands
            time_since_last = time.time() - self._last_command_time
            if time_since_last < 0.5:
                time.sleep(0.5 - time_since_last)

            for attempt in range(2):
                try:
                    # Open USB device by physical port path.
                    # device_path must be e.g. '1-1.1', '1-1.3', '1-1.4'.
                    with USBConnection(port_path=self._device_path, timeout=5000) as ser:
                        # Prepare command
                        crc = self._get_crc(command)
                        full_command = command.encode() + crc + b'\r'
                        _LOGGER.debug(f'Sending command: {command} ({full_command})')

                        # Flush buffers
                        ser.reset_input_buffer()
                        ser.reset_output_buffer()

                        # Write
                        ser.write(full_command)

                        # Read response until CR
                        response = ser.read_until(b'\r')

                    if not response:
                        raise Exception("No response from inverter")

                    # Strip trailing CR
                    if response.endswith(b'\r'):
                        response = response[:-1]

                    # Check for ACK/NAK
                    if response == b'(ACK' or response == b'ACK':
                        return 'ACK'
                    if response == b'(NAK' or response == b'NAK':
                        if attempt == 0:
                            _LOGGER.warning(f"Got NAK for command {command}, retrying in 1s...")
                            time.sleep(1)
                            continue
                        raise Exception(f"Command \"{command}\" not supported")

                    # Extract CRC and Data
                    valid_chars = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 (.-:")
                    if len(response) > 2:
                        # Strategy 1: Standard Split (Last 2 bytes are CRC)
                        std_data = response[:-2]
                        std_crc_received = response[-2:]
                        std_crc_calc = self._get_crc(std_data)

                        if std_crc_calc == std_crc_received:
                            raw_data = std_data
                        else:
                            # Strategy 2: Smart Scan
                            found_smart = False
                            for i in range(len(response) - 2, 0, -1):
                                candidate_data = response[:i]
                                if all(b in valid_chars for b in candidate_data):
                                    candidate_crc = response[i:i + 2]
                                    if self._get_crc(candidate_data) == candidate_crc:
                                        _LOGGER.debug(
                                            f"Smart CRC scan recovered data for {command}. "
                                            f"Garbage detected at end of response."
                                        )
                                        raw_data = candidate_data
                                        found_smart = True
                                        break

                            if not found_smart:
                                _LOGGER.warning(
                                    f"CRC mismatch for {command}: "
                                    f"Recv {std_crc_received.hex()} vs Calc {std_crc_calc.hex()}. "
                                    f"Smart scan failed to recover."
                                )
                                raw_data = std_data
                    else:
                        raw_data = response

                    try:
                        decoded_response = raw_data.decode('iso-8859-1', errors='ignore')
                        decoded_response = decoded_response.replace('\x00', '').strip()
                    except Exception:
                        decoded_response = raw_data.decode('utf-8', errors='ignore').replace('\x00', '').strip()

                    if '(' in decoded_response:
                        decoded_response = decoded_response[decoded_response.find('(') + 1:]

                    _LOGGER.debug(f'Response from inverter: {decoded_response}')
                    return decoded_response

                except Exception as e:
                    if attempt == 1:
                        _LOGGER.error(f"Failed to communicate with inverter after retries: {e}")
                        raise e
                    else:
                        _LOGGER.warning(f"Failed to communicate with inverter: {e}")
                    time.sleep(0.5)
                finally:
                    self._last_command_time = time.time()

    def get_general_status(self) -> dict:
        """Get general status parameters (QPIGS)."""
        raw = self.send_command("QPIGS")
        if not raw:
            return {}

        parts = raw.split()
        if len(parts) < 16:
            _LOGGER.warning(f"QPIGS response too short: {raw}")
            return {}

        try:
            data = {
                "grid_voltage": float(parts[0]),
                "grid_frequency": float(parts[1]),
                "ac_output_voltage": float(parts[2]),
                "ac_output_frequency": float(parts[3]),
                "ac_output_apparent_power": int(parts[4]),
                "ac_output_active_power": int(parts[5]),
                "output_load_percent": int(parts[6]),
                "bus_voltage": int(parts[7]),
                "battery_voltage": float(parts[8]),
                "battery_charging_current": int(parts[9]),
                "battery_capacity": int(parts[10]),
                "heat_sink_temperature": int(parts[11]),
                "pv_input_current": float(parts[12]),
                "pv_input_voltage": float(parts[13]),
                "scc_voltage": float(parts[14]),
                "battery_discharge_current": int(parts[15]),
                "status_binary": parts[16],
            }
            if len(parts) > 16:
                data["pv_charging_power"] = int(parts[19])
            return data
        except (ValueError, IndexError) as e:
            _LOGGER.error(f"Error parsing QPIGS data: {e} | Raw: {raw}")
            return {}

    def get_warnings(self) -> str:
        """Get warning status (QPIWS)."""
        try:
            return self.send_command("QPIWS")
        except Exception as e:
            _LOGGER.error(f"Error getting warnings: {e}")
            return ""

    def get_mode(self) -> str:
        """Get Device Mode (QMOD)."""
        return self.send_command("QMOD")

    def get_device_id(self) -> str:
        """Get Device ID (QID)."""
        return self.send_command("QID")

    def set_ac_input_range(self, mode_code: str) -> bool:
        """Set AC Input Range. PGR00 or PGR01."""
        resp = self.send_command(mode_code)
        return "ACK" in resp

    def get_rated_information(self) -> dict:
        """Get Rated Information (QPIRI)."""
        raw = self.send_command("QPIRI")
        if not raw:
            return {}

        parts = raw.split()
        if len(parts) < 17:
            _LOGGER.warning(f"QPIRI response too short: {raw}")
            return {}

        try:
            data = {}
            if len(parts) > 16:
                data["output_source_priority"] = parts[16]
            if len(parts) > 17:
                data["charger_source_priority"] = parts[17]
            if len(parts) > 9:
                data["battery_cutoff_voltage"] = float(parts[9])
            if len(parts) > 10:
                data["battery_bulk_voltage"] = float(parts[10])
            if len(parts) > 11:
                data["battery_float_voltage"] = float(parts[11])
            if len(parts) > 12:
                data["battery_type"] = parts[12]
            if len(parts) > 13:
                data["max_ac_charging_current"] = int(parts[13])
            if len(parts) > 14:
                data["max_charging_current"] = int(parts[14])
            if len(parts) > 15:
                data["ac_input_range"] = parts[15]
            if len(parts) > 19:
                data["machine_type"] = parts[19]
            return data
        except Exception as e:
            _LOGGER.error(f"Error parsing QPIRI: {e}")
            return {}

    def set_output_source_priority(self, priority: str) -> bool:
        """Set Output Source Priority. 00/01/02."""
        return "ACK" in self.send_command(f"POP{priority}")

    def set_charger_source_priority(self, priority: str) -> bool:
        """Set Charger Source Priority. 00/01/02/03."""
        return "ACK" in self.send_command(f"PCP{priority}")

    def set_max_charging_current(self, current: int) -> bool:
        """Set Max Charging Current. MNCHGC<nnn>."""
        cmd = f"MNCHGC{current:03}"
        return "ACK" in self.send_command(cmd)

    def set_max_utility_charging_current(self, current: int) -> bool:
        """Set Max Utility Charging Current. MUCHGC<nnn>."""
        cmd = f"MUCHGC{current:03}"
        return "ACK" in self.send_command(cmd)

    def set_battery_type(self, batt_type: str) -> bool:
        """Set Battery Type. PBT<nn>. 00:AGM, 01:Flooded, 02:User."""
        cmd = f"PBT{batt_type}"
        return "ACK" in self.send_command(cmd)

    def set_battery_cutoff_voltage(self, voltage: float) -> bool:
        """Set Battery Cut-off Voltage. PSDV<nn.n>."""
        cmd = f"PSDV{voltage:04.1f}"
        return "ACK" in self.send_command(cmd)

    def set_battery_bulk_voltage(self, voltage: float) -> bool:
        """Set Battery Bulk (C.V.) Voltage. PCVV<nn.n>."""
        cmd = f"PCVV{voltage:04.1f}"
        return "ACK" in self.send_command(cmd)

    def set_battery_float_voltage(self, voltage: float) -> bool:
        """Set Battery Float Voltage. PBFT<nn.n>."""
        cmd = f"PBFT{voltage:04.1f}"
        return "ACK" in self.send_command(cmd)

    def get_firmware_version(self) -> str:
        """Get Main CPU Firmware Version (QVFW)."""
        try:
            raw = self.send_command("QVFW")
            if "VERFW:" in raw:
                return raw.split("VERFW:")[1]
            return raw
        except Exception:
            return "Unknown"

    def get_model_id(self) -> str | None:
        """Get Model Name ID (QGMN)."""
        try:
            return self.send_command("QGMN")
        except Exception:
            return None

    def get_model_name(self) -> str | None:
        """Get Model Name (QGMN)."""
        try:
            raw = self.get_model_id()
            if not raw:
                return None
            code = raw.replace('(', '').strip()
            mapping = {
                "001": "VP-5000",
                "002": "VM-5000",
                "003": "VP-3000",
                "004": "VM-3000",
                "005": "MKS+-2000-48-LV-LY",
                "006": "Axpert MLV 3K-24",
                "007": "Axpert PLV 3K-24",
                "008": "Axpert MKS 3KP",
                "009": "Axpert KS 3KP",
                "010": "Axpert MKS 5KP",
                "011": "Axpert KS 5KP",
                "012": "Axpert MKS 4K/5K 64VDC",
                "013": "Axpert KS 4K/5K 64VDC",
                "014": "Axpert MKS 4K/5K",
                "015": "Axpert KS 4K/5K",
                "016": "ALFA M-5000",
                "017": "ALFA P-5000",
                "018": "Axpert Plus Duo/Tri 5KVA",
                "019": "Axpert EPS 5KW",
                "020": "Axpert EPS M-5KW",
                "021": "Axpert EPS 33-5KW",
                "022": "Axpert MKS II 5KW",
                "023": "AXPERT KING 5KW",
                "024": "AXPERT KING 3KW",
                "025": "APT MKS II 5KW (Feed-in grid)",
                "026": "Axpert MLV 5KW-48V",
                "027": "AXPERT VMIII",
                "028": "APT VMIII 3.2KW (Feed-in grid)",
                "029": "AXPERT VMII",
                "030": "Fusion VMII (Feed-in grid)",
                "031": "Phocos MKS II 5KW",
                "032": "Axpert MKS Zero LV 0.7KW",
                "033": "Axpert MKS Zero LV 1.4KW",
                "034": "Axpert MKS Zero LV 2.6KW",
                "035": "AXPERT KING 5KW (Energy)",
                "036": "AXPERT KING 3KW (Energy)",
                "037": "AXPERT VMIII (Energy)",
                "038": "Phocos MKS II 5KW (Energy)",
                "039": "Phocos MKS II 5KW LV",
                "040": "Axpert SE 3.5K",
                "041": "Axpert SE 5.5K",
                "042": "AXPERT MKS III 5KW",
                "043": "MAX 3.6K",
                "044": "MAX 7.2K",
                "045": "MAX 5K LV",
            }
            return mapping.get(code, code)
        except Exception:
            return None
