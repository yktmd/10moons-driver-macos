"""
Copyright (C) 2019-2025 Alexandr Vasilyev, f-caro, Fern Lane and other
10moons-driver and this fork contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

#import argparse
import time
import logging
from array import array
from typing import Any

import usb
import yaml
import Quartz

__version__ = "0.1.1"

CONFIG_DEFAULT_PATH = "config.yaml"
LOGGING_FMT = "[%(asctime)s] [%(levelname).1s] [%(lineno)3s] %(message)s"
LOGGING_DATEFMT = "%Y-%m-%d %H:%M:%S"


def _parse_config(config_path: str) -> dict[str, Any]:
    """Parses config from YAML file

    Args:
        config_path (str): path to config file

    Returns:
        dict[str, Any]: parsed config as dictionary object
    """
    with open(config_path, "r", encoding="utf-8") as file_io:
        config = yaml.load(file_io, yaml.FullLoader)
    return config

def _prepare_device(
    vendor_id: int, product_id: int, reports: list[dict[str | int, list[int]]]
) -> tuple[usb.core.Device, usb.core.Endpoint]:
    """Finds and resets USB device

    Args:
        vendor_id (int): tablet's usb vendor ID (from lsusb)
        product_id (int): tablet's usb product ID (from lsusb)
        reports (list[dict[str | int, list[int]]]): array of SET_REPORTs from config

    Raises:
        Exception: in case of error (eg. insufficient permissions)

    Returns:
        tuple[usb.core.Device, usb.core.Endpoint]: usb device, interface endpoint (interfaces()[1].endpoints()[0])
    """
    # Find the device
    dev = usb.core.find(idVendor=vendor_id, idProduct=product_id)
    logging.debug(str(dev))

    # Check instance type
    if not isinstance(dev, usb.core.Device):
        raise Exception("USB device instance is not usb.core.Device type")

    # Select end point for reading second interface [2] for actual data
    # Interface[0] associated Internal USB storage (labelled as CDROM drive)
    # Interface[1] useful to map 'Full Tablet Active Area' -- outputs 64 bytes of xinput events
    # Interface[2] maps to the 'AndroidActive Area' -- outputs 8 bytes of xinput events

    # Reset the device (don't know why, but till it works don't touch it)
    logging.info("Resetting USB device")
    try:
        dev.reset()
    except usb.core.USBError as e:
        logging.warning(f"Sıfırlama atlandı (macOS kısıtlaması olabilir): {e}")

    # Drop default kernel driver from all devices
    for iface_id in [0, 1, 2]:
        try:
            if dev.is_kernel_driver_active(iface_id):
                dev.detach_kernel_driver(iface_id)
        except (NotImplementedError, usb.core.USBError):
            logging.debug(f"Detaching kernel driver from interface: {iface_id}")

    # Set new configuration
    logging.info("Setting new configuration to USB device")
    try:
        dev.set_configuration()
    except usb.core.USBError as e:
        logging.warning(f"Konfigürasyon atlandı (Cihaz zaten ayarlı olabilir): {e}")

    # Claim interface
    # Like in 10moons-tools: <https://github.com/DIGImend/10moons-tools>
    interface = 2
    logging.info(f"Claiming USB interface {interface}")
    usb.util.claim_interface(dev, interface)

    def _set_report(w_value, report_data) -> None:
        # Host to device, Class, Interface; SET_REPORT
        logging.debug(f"Sending SET_REPORT: {w_value}, {report_data}")
        dev.ctrl_transfer(0x21, 9, w_value, interface, report_data, timeout=250)

    # Send specific reports
    # From 10moons-tools: <https://github.com/DIGImend/10moons-tools>
    logging.info("Sending reports")
    for report in reports:
        for w_value, report_data in report.items():
            if isinstance(w_value, str):
                w_value = int(w_value)
            _set_report(w_value, report_data)

    # Find endpoint
    endpoint = dev[0].interfaces()[1].endpoints()[0]
    logging.debug(str(endpoint))

    return dev, endpoint

def send_mouse_event(x, y, pressure_norm, is_touching, was_touching):
    if is_touching and not was_touching:
        event_type = Quartz.kCGEventLeftMouseDown
    elif not is_touching and was_touching:
        event_type = Quartz.kCGEventLeftMouseUp
    elif is_touching:
        event_type = Quartz.kCGEventLeftMouseDragged
    else:
        event_type = Quartz.kCGEventMouseMoved

    event = Quartz.CGEventCreateMouseEvent(None, event_type, (x, y), Quartz.kCGMouseButtonLeft)

    # we say to macos that this is a tablet event by setting the mouse event subtype to tablet point (1) and adding pressure data
    try:
        tablet_subtype = Quartz.kCGEventMouseSubtypeTabletPoint
    except AttributeError:
        tablet_subtype = 1
        
    Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventSubtype, tablet_subtype)
    
    Quartz.CGEventSetDoubleValueField(event, Quartz.kCGMouseEventPressure, pressure_norm)

    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def main() -> None:
    logging.basicConfig(format=LOGGING_FMT, datefmt=LOGGING_DATEFMT, level=logging.DEBUG)
    config = _parse_config(CONFIG_DEFAULT_PATH)

    pen_config = config.get("pen", {})
    max_x = pen_config.get("max_x", 4095)
    max_y = pen_config.get("max_y", 4095)
    pressure_in_min = pen_config.get("pressure_in_min", 0)
    pressure_in_max = pen_config.get("pressure_in_max", 2047)
    pressure_threshold_press = pen_config.get("pressure_threshold_press", 300)
    pressure_threshold_release = pen_config.get("pressure_threshold_release", 200)

    # We get the main display size to map the tablet coordinates to screen coordinates
    main_monitor = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
    screen_width = Quartz.CGRectGetWidth(main_monitor)
    screen_height = Quartz.CGRectGetHeight(main_monitor)

    logging.info("Driver initialized. Searching for device (Automatic reconnection enabled). Press CTRL+C to exit.")

    # main loop
    while True:
        try:
            try:
                dev, endpoint = _prepare_device(config["vendor_id"], config["product_id"], config["reports"])
                logging.info("Tablet connected! Starting data stream...")
            except Exception as e:
                time.sleep(5)
                continue

            touch = False

            while True:
                try:
                    data = dev.read(endpoint.bEndpointAddress, endpoint.wMaxPacketSize)
                    
                    if len(data) > 5 and data[5] not in [3, 4, 5, 6]:
                        continue

                    raw_x = int.from_bytes(data[1:3], byteorder="big")
                    raw_y = int.from_bytes(data[3:5], byteorder="big")
                    pressure_raw = int.from_bytes(data[5:7], byteorder="big")

                    mapped_x = (raw_x / max_x) * screen_width
                    mapped_y = (raw_y / max_y) * screen_height

                    pressure_out_max = 2047
                    pressure_out_min = 0
                    
                    calc_pressure = (pressure_raw - pressure_in_min) * (pressure_out_max - pressure_out_min) / (pressure_in_max - pressure_in_min) + pressure_out_min
                    calc_pressure = max(pressure_out_min, min(calc_pressure, pressure_out_max))

                    pressure_norm = calc_pressure / pressure_out_max

                    was_touching = touch

                    if not touch and calc_pressure > pressure_threshold_press:
                        touch = True
                    elif touch and calc_pressure < pressure_threshold_release:
                        touch = False

                    send_mouse_event(mapped_x, mapped_y, pressure_norm, touch, was_touching)

                except usb.core.USBError as e:
                    # Timeout errors are expected so we just ignore them and continue reading
                    if e.args[0] == 60: # USB timeout error code is 60 for macOS
                        continue
                    
                    logging.warning(f"Connection lost (USB Error: {e}). Trying to reconnect...")
                    break

        except KeyboardInterrupt:
            logging.info("Exiting...")
            break
        except Exception as e:
            logging.error(f"Unexpected error occurred: {e}")


if __name__ == "__main__":
    main()
