#!/usr/bin/env python3
import os, sys, time, json, random, logging, threading, subprocess
import dbus, dbus.service, dbus.mainloop.glib
from gi.repository import GLib

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
MOCK_MODE = True
BOOT_DIR = "/boot/firmware" if os.path.exists("/boot/firmware") else "/boot"
CONFIG_FILE = os.path.join(BOOT_DIR, "tpms_config.json")

UART_SVC_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
UART_RX_UUID  = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
UART_TX_UUID  = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

BLUEZ_SERVICE_NAME = "org.bluez"
GATT_MANAGER_IFACE = "org.bluez.GattManager1"
DBUS_OM_IFACE      = "org.freedesktop.DBus.ObjectManager"
DBUS_PROP_IFACE    = "org.freedesktop.DBus.Properties"
GATT_SERVICE_IFACE = "org.bluez.GattService1"
GATT_CHRC_IFACE    = "org.bluez.GattCharacteristic1"
LE_ADVERTISING_MANAGER_IFACE = "org.bluez.LEAdvertisingManager1"
LE_ADVERTISEMENT_IFACE       = "org.bluez.LEAdvertisement1"

state_lock = threading.Lock()
APP_STATE = "MONITOR"
SYSTEM_MODE = "HIGHWAY"
sdr_healthy = True if MOCK_MODE else False
config_data = {"active_profile": "prado_150", "profiles": {}}
tires, wizard_step_idx, wizard_bonded = {}, 0, {}
tx_characteristic = None

def load_config():
    global config_data
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f: config_data = json.load(f)
            reset_tire_data()
            return True
        except Exception as e: logging.error(f"Config load error: {e}")
    return False

def persist_config():
    try:
        subprocess.run(["sudo", "mount", "-o", "remount,rw", BOOT_DIR], check=False)
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(config_data, f, indent=2)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, CONFIG_FILE)
    except Exception as e: logging.error(f"Persist error: {e}")
    finally: subprocess.run(["sudo", "mount", "-o", "remount,ro", BOOT_DIR], check=False)

def get_active_profile():
    return config_data["profiles"].get(config_data.get("active_profile"), {})

def reset_tire_data():
    global tires
    prof = get_active_profile()
    wheels = prof.get("wheels", ["FL", "FR", "RR", "RL", "SP"])
    base_p = prof.get("thresholds", {}).get("target_psi", 35.0)
    now = time.time()
    tires = {w: {"psi": base_p + round(random.uniform(-0.3, 0.3), 1), "temp": 24, "last_seen": now, "status": "NORMAL"} for w in wheels}

class Characteristic(dbus.service.Object):
    def __init__(self, bus, index, uuid, flags, service):
        self.path = f"{service.path}/char{index}"
        self.bus, self.uuid, self.service, self.flags, self.notifying = bus, uuid, service, flags, False
        super().__init__(bus, self.path)
    def get_properties(self):
        return {GATT_CHRC_IFACE: {'Service': self.service.get_path(), 'UUID': self.uuid, 'Flags': self.flags}}
    def get_path(self): return dbus.ObjectPath(self.path)
    @dbus.service.method(DBUS_PROP_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        if interface != GATT_CHRC_IFACE: raise dbus.exceptions.DBusException('org.freedesktop.DBus.Error.InvalidArgs')
        return self.get_properties()[GATT_CHRC_IFACE]
    @dbus.service.method(GATT_CHRC_IFACE)
    def StartNotify(self):
        self.notifying = True
        logging.info("BLE Client Subscribed! Sending initial telemetry frame...")
        threading.Thread(target=broadcast_telemetry, daemon=True).start()
    @dbus.service.method(GATT_CHRC_IFACE)
    def StopNotify(self):
        self.notifying = False
        logging.info("BLE Client Unsubscribed")

class UartTxCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        super().__init__(bus, index, UART_TX_UUID, ['notify'], service)

    def send_notification(self, text):
        if not self.notifying: return
        data = (text if text.endswith('\n') else text + '\n').encode('utf-8')
        # Standard BLE ATT MTU chunking (20-byte safe packets)
        chunk_size = 20
        for i in range(0, len(data), chunk_size):
            chunk = data[i:i + chunk_size]
            try:
                self.PropertiesChanged(GATT_CHRC_IFACE, {'Value': dbus.ByteArray(chunk)}, [])
            except Exception as e:
                logging.error(f"Error sending BLE chunk: {e}")
                break
            time.sleep(0.012) # 12ms pacing prevents BlueZ queue saturation

    @dbus.service.signal(DBUS_PROP_IFACE, signature='sa{sv}as')
    def PropertiesChanged(self, interface, changed, invalidated): pass

class UartRxCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        super().__init__(bus, index, UART_RX_UUID, ['write', 'write-without-response'], service)

    @dbus.service.method(GATT_CHRC_IFACE, in_signature='aya{sv}')
    def WriteValue(self, value, options):
        try:
            cmd = bytes(value).decode('utf-8').strip()
            logging.info(f"Incoming BLE Command: {cmd}")
            handle_incoming_command(cmd)
        except Exception as e:
            logging.error(f"Error executing write: {e}")

class UartService(dbus.service.Object):
    def __init__(self, bus, index):
        global tx_characteristic
        self.path, self.bus, self.uuid, self.primary = f"/org/bluez/service{index}", bus, UART_SVC_UUID, True
        self.characteristics = []
        super().__init__(bus, self.path)
        tx_characteristic = UartTxCharacteristic(bus, 0, self)
        self.characteristics.extend([tx_characteristic, UartRxCharacteristic(bus, 1, self)])

    def get_properties(self):
        return {GATT_SERVICE_IFACE: {'UUID': self.uuid, 'Primary': self.primary, 'Characteristics': dbus.Array([c.get_path() for c in self.characteristics], signature='o')}}
    def get_path(self): return dbus.ObjectPath(self.path)
    @dbus.service.method(DBUS_PROP_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        if interface != GATT_SERVICE_IFACE: raise dbus.exceptions.DBusException('org.freedesktop.DBus.Error.InvalidArgs')
        return self.get_properties()[GATT_SERVICE_IFACE]

class BleApplication(dbus.service.Object):
    def __init__(self, bus):
        self.path, self.services = '/', [UartService(bus, 0)]
        super().__init__(bus, self.path)
    def get_path(self): return dbus.ObjectPath(self.path)
    @dbus.service.method(DBUS_OM_IFACE, out_signature='a{oa{sa{sv}}}')
    def GetManagedObjects(self):
        res = {}
        for s in self.services:
            res[s.get_path()] = s.get_properties()
            for c in s.characteristics: res[c.get_path()] = c.get_properties()
        return res

class BleAdvertisement(dbus.service.Object):
    def __init__(self, bus, index):
        self.path, self.bus, self.ad_type, self.service_uuids, self.local_name = f"/org/bluez/advertisement{index}", bus, "peripheral", [UART_SVC_UUID], "LC150-TPMS"
        super().__init__(bus, self.path)
    def get_path(self): return dbus.ObjectPath(self.path)
    def get_properties(self):
        return {LE_ADVERTISEMENT_IFACE: {'Type': self.ad_type, 'ServiceUUIDs': dbus.Array(self.service_uuids, signature='s'), 'LocalName': dbus.String(self.local_name)}}
    @dbus.service.method(DBUS_PROP_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        if interface != LE_ADVERTISEMENT_IFACE: raise dbus.exceptions.DBusException('org.freedesktop.DBus.Error.InvalidArgs')
        return self.get_properties()[LE_ADVERTISEMENT_IFACE]
    @dbus.service.method(LE_ADVERTISEMENT_IFACE, in_signature='', out_signature='')
    def Release(self): pass

def handle_incoming_command(cmd):
    global APP_STATE, SYSTEM_MODE, wizard_step_idx
    with state_lock:
        if cmd.startswith("PROFILE:SELECT:"):
            pid = cmd.split(":")[-1]
            if pid in config_data.get("profiles", {}):
                config_data["active_profile"] = pid
                SYSTEM_MODE = "HIGHWAY"
                reset_tire_data(); persist_config()
                logging.info(f"Switched profile to: {pid}")

        elif cmd == "WIZARD:START":
            APP_STATE = "WIZARD"
            wizard_step_idx = 0
            wizard_bonded.clear()
            logging.info("Pairing Wizard Started")

        elif cmd == "WIZARD:CANCEL":
            APP_STATE = "MONITOR"
            logging.info("Pairing Wizard Cancelled")

        elif cmd == "SIM:TOGGLE_MODE":
            SYSTEM_MODE = "OFFROAD" if SYSTEM_MODE == "HIGHWAY" else "HIGHWAY"
            prof = get_active_profile()
            tgt = 18.0 if SYSTEM_MODE == "OFFROAD" else prof.get("thresholds", {}).get("target_psi", 35.0)
            for w in tires:
                if w != "SP": tires[w]["psi"] = tgt + round(random.uniform(-0.3, 0.3), 1)
            logging.info(f"Toggled Mode to: {SYSTEM_MODE}")

        elif cmd == "SIM:DEFLATE_STEP":
            if APP_STATE == "WIZARD":
                wheels = get_active_profile().get("wheels", [])
                if wizard_step_idx < len(wheels):
                    w = wheels[wizard_step_idx]
                    wizard_bonded[w] = f"0{random.randint(10,99)}x{random.randint(10,99)}"
                    wizard_step_idx += 1
                    logging.info(f"Bonded wheel {w}. Next index: {wizard_step_idx}")
                    if wizard_step_idx >= len(wheels):
                        get_active_profile()["sensors"] = wizard_bonded
                        persist_config(); reset_tire_data(); APP_STATE = "MONITOR"
                        logging.info("Wizard complete. Active profile updated.")
    broadcast_telemetry()

def broadcast_telemetry():
    if not tx_characteristic: return
    with state_lock:
        prof = get_active_profile()
        p_list = [{"id": k, "name": v.get("name", k), "layout": v.get("layout", "4W")} for k, v in config_data.get("profiles", {}).items()]
        tires_clean = {w: {"psi": round(tires[w]["psi"], 1), "temp": tires[w]["temp"]} for w in tires}
        
        if APP_STATE == "WIZARD":
            wheels = prof.get("wheels", [])
            payload = {
                "state": "WIZARD",
                "profile": config_data.get("active_profile"),
                "step": wheels[wizard_step_idx] if wizard_step_idx < len(wheels) else "DONE",
                "step_num": wizard_step_idx + 1,
                "total_steps": len(wheels),
                "asset": prof.get("asset", "lc150_2015"),
                "layout": prof.get("layout", "5W")
            }
        else:
            payload = {
                "state": "MONITOR",
                "mode": SYSTEM_MODE,
                "profile": config_data.get("active_profile"),
                "name": prof.get("name", "Vehicle"),
                "asset": prof.get("asset", "lc150_2015"),
                "layout": prof.get("layout", "5W"),
                "profiles": p_list,
                "tires": tires_clean
            }
    msg = json.dumps(payload) + "\n"
    tx_characteristic.send_notification(msg)

def mock_telemetry_loop():
    while True:
        time.sleep(2.0)
        with state_lock:
            if APP_STATE == "MONITOR":
                for w in tires: tires[w]["psi"] = round(tires[w]["psi"] + random.uniform(-0.1, 0.1), 1)
        broadcast_telemetry()

def main():
    load_config()
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    adapter_path = "/org/bluez/hci0"
    adapter_props = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, adapter_path), DBUS_PROP_IFACE)
    adapter_props.Set("org.bluez.Adapter1", "Powered", dbus.Boolean(True))

    service_manager = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, adapter_path), GATT_MANAGER_IFACE)
    ad_manager = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, adapter_path), LE_ADVERTISING_MANAGER_IFACE)
    app, ad = BleApplication(bus), BleAdvertisement(bus, 0)
    service_manager.RegisterApplication(app.get_path(), {}, reply_handler=lambda: logging.info("GATT Registered"), error_handler=lambda e: logging.error(f"GATT Error: {e}"))
    ad_manager.RegisterAdvertisement(ad.get_path(), {}, reply_handler=lambda: logging.info("BLE Advertising Active"), error_handler=lambda e: logging.error(f"Ad Error: {e}"))

    threading.Thread(target=mock_telemetry_loop, daemon=True).start()
    logging.info("Daemon ready for BLE streaming.")
    GLib.MainLoop().run()

if __name__ == "__main__": main()
