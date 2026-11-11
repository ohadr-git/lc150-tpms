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

DEFAULT_CONFIG = {
  "active_profile": "prado_150",
  "profiles": {
    "prado_150": {
      "name": "Land Cruiser 150",
      "layout": "5W",
      "asset": "lc150_2015",
      "wheels": ["FL", "FR", "RR", "RL", "SP"],
      "sensors": {"04d3c1": "FL", "04d3c2": "FR", "04d3c3": "RR", "04d3c4": "RL", "04d3c5": "SP"},
      "thresholds": {"target_psi": 35.0, "highway_low_psi": 28.0, "highway_check_psi": 31.0, "offroad_enabled": True, "offroad_enter_psi": 24.0, "offroad_exit_psi": 29.0, "trail_min_safe_psi": 12.0, "trail_max_delta": 5.0}
    },
    "subaru_sedan": {
      "name": "Daily Sedan",
      "layout": "4W",
      "asset": "sedan",
      "wheels": ["FL", "FR", "RR", "RL"],
      "sensors": {"01a1b1": "FL", "01a1b2": "FR", "01a1b3": "RR", "01a1b4": "RL"},
      "thresholds": {"target_psi": 33.0, "highway_low_psi": 27.0, "highway_check_psi": 30.0, "offroad_enabled": False, "offroad_enter_psi": 0.0, "offroad_exit_psi": 0.0, "trail_min_safe_psi": 0.0, "trail_max_delta": 0.0}
    },
    "moto_dirt": {
      "name": "KTM 450 Motocross",
      "layout": "2W_MOTO",
      "asset": "motocross",
      "wheels": ["FRONT", "REAR"],
      "sensors": {"02b2c1": "FRONT", "02b2c2": "REAR"},
      "thresholds": {"target_psi": 15.0, "highway_low_psi": 11.0, "highway_check_psi": 13.0, "offroad_enabled": False, "offroad_enter_psi": 0.0, "offroad_exit_psi": 0.0, "trail_min_safe_psi": 0.0, "trail_max_delta": 0.0}
    },
    "box_trailer": {
      "name": "Utility Trailer",
      "layout": "2W_TRAILER",
      "asset": "trailer",
      "wheels": ["LEFT", "RIGHT"],
      "sensors": {"03c3d1": "LEFT", "03c3d2": "RIGHT"},
      "thresholds": {"target_psi": 50.0, "highway_low_psi": 42.0, "highway_check_psi": 46.0, "offroad_enabled": False, "offroad_enter_psi": 0.0, "offroad_exit_psi": 0.0, "trail_min_safe_psi": 0.0, "trail_max_delta": 0.0}
    }
  }
}

def load_config():
    global config_data
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                config_data = json.load(f)
            reset_tire_data()
            logging.info(f"Loaded config: Active={config_data.get('active_profile')}")
            return True
        except Exception as e:
            logging.error(f"Config load error: {e}")
    config_data = DEFAULT_CONFIG
    persist_config()
    reset_tire_data()
    return True

def persist_config():
    try:
        subprocess.run(["sudo", "mount", "-o", "remount,rw", BOOT_DIR], check=False)
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(config_data, f, indent=2)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, CONFIG_FILE)
        logging.info("Config committed to boot partition")
    except Exception as e:
        logging.error(f"Persist error: {e}")
    finally:
        subprocess.run(["sudo", "mount", "-o", "remount,ro", BOOT_DIR], check=False)

def get_active_profile():
    return config_data["profiles"].get(config_data.get("active_profile"), {})

def reset_tire_data():
    global tires
    prof = get_active_profile()
    wheels = prof.get("wheels", ["FL", "FR", "RR", "RL"])
    base_p = prof.get("thresholds", {}).get("target_psi", 35.0)
    now = time.time()
    tires = {w: {"psi": round(base_p + random.uniform(-0.3, 0.3), 1), "temp": 24, "last_seen": now, "status": "NORMAL"} for w in wheels}

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
        logging.info("Client subscribed. Emitting state...")
        threading.Thread(target=broadcast_telemetry, daemon=True).start()
    @dbus.service.method(GATT_CHRC_IFACE)
    def StopNotify(self):
        self.notifying = False

class UartTxCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        super().__init__(bus, index, UART_TX_UUID, ['notify'], service)
    def send_notification(self, text):
        if not self.notifying: return
        data = (text if text.endswith('\n') else text + '\n').encode('utf-8')
        for i in range(0, len(data), 20):
            chunk = data[i:i + 20]
            try:
                self.PropertiesChanged(GATT_CHRC_IFACE, {'Value': dbus.ByteArray(chunk)}, [])
            except Exception as e:
                logging.error(f"BLE send err: {e}")
                break
            time.sleep(0.012)
    @dbus.service.signal(DBUS_PROP_IFACE, signature='sa{sv}as')
    def PropertiesChanged(self, interface, changed, invalidated): pass

class UartRxCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        super().__init__(bus, index, UART_RX_UUID, ['write', 'write-without-response'], service)
    @dbus.service.method(GATT_CHRC_IFACE, in_signature='aya{sv}')
    def WriteValue(self, value, options):
        try:
            cmd = bytes(value).decode('utf-8').strip()
            handle_incoming_command(cmd)
        except Exception as e:
            logging.error(f"BLE cmd execution err: {e}")

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

        elif cmd.startswith("PROFILE:CREATE:"):
            try:
                payload = json.loads(cmd[len("PROFILE:CREATE:"):])
                pid = payload["id"]
                config_data["profiles"][pid] = payload
                config_data["active_profile"] = pid
                reset_tire_data(); persist_config()
            except Exception as e:
                logging.error(f"PROFILE:CREATE err: {e}")

        elif cmd.startswith("PROFILE:DELETE:"):
            pid = cmd.split(":")[-1]
            if pid in config_data.get("profiles", {}) and len(config_data["profiles"]) > 1:
                del config_data["profiles"][pid]
                if config_data["active_profile"] == pid:
                    config_data["active_profile"] = list(config_data["profiles"].keys())[0]
                reset_tire_data(); persist_config()

        elif cmd.startswith("PROFILE:THRESHOLDS:"):
            try:
                payload = json.loads(cmd[len("PROFILE:THRESHOLDS:"):])
                prof = get_active_profile()
                prof["thresholds"].update(payload)
                persist_config()
            except Exception as e:
                logging.error(f"PROFILE:THRESHOLDS err: {e}")

        elif cmd.startswith("TIRES:ROTATE:"):
            try:
                swap_map = json.loads(cmd[len("TIRES:ROTATE:"):])
                prof = get_active_profile()
                old_sensors = prof.get("sensors", {})
                new_sensors = {}
                for s_id, old_pos in old_sensors.items():
                    new_sensors[s_id] = swap_map.get(old_pos, old_pos)
                prof["sensors"] = new_sensors
                persist_config()
                logging.info(f"Rotated tire mapping: {new_sensors}")
            except Exception as e:
                logging.error(f"TIRES:ROTATE err: {e}")

        elif cmd == "WIZARD:START":
            APP_STATE = "WIZARD"
            wizard_step_idx = 0
            wizard_bonded.clear()

        elif cmd == "WIZARD:CANCEL":
            APP_STATE = "MONITOR"

        elif cmd == "SIM:TOGGLE_MODE":
            SYSTEM_MODE = "OFFROAD" if SYSTEM_MODE == "HIGHWAY" else "HIGHWAY"
            prof = get_active_profile()
            tgt = 18.0 if SYSTEM_MODE == "OFFROAD" else prof.get("thresholds", {}).get("target_psi", 35.0)
            for w in tires:
                if w != "SP":
                    tires[w]["psi"] = tgt + round(random.uniform(-0.3, 0.3), 1)

        elif cmd == "SIM:DEFLATE_STEP":
            if APP_STATE == "WIZARD":
                wheels = get_active_profile().get("wheels", [])
                if wizard_step_idx < len(wheels):
                    w = wheels[wizard_step_idx]
                    wizard_bonded[f"mock_{random.randint(1000, 9999)}"] = w
                    wizard_step_idx += 1
                    if wizard_step_idx >= len(wheels):
                        get_active_profile()["sensors"] = wizard_bonded
                        persist_config(); reset_tire_data(); APP_STATE = "MONITOR"

    broadcast_telemetry()

def broadcast_telemetry():
    if not tx_characteristic: return
    with state_lock:
        prof = get_active_profile()
        p_list = [{"id": k, "name": v.get("name", k), "layout": v.get("layout", "4W"), "asset": v.get("asset", "lc150_2015")} for k, v in config_data.get("profiles", {}).items()]
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
                "wheels": prof.get("wheels", ["FL", "FR", "RR", "RL"]),
                "thresholds": prof.get("thresholds", {}),
                "profiles": p_list,
                "tires": tires_clean
            }
    tx_characteristic.send_notification(json.dumps(payload) + "\n")

def mock_telemetry_loop():
    while True:
        time.sleep(2.0)
        with state_lock:
            if APP_STATE == "MONITOR":
                for w in tires:
                    tires[w]["psi"] = round(tires[w]["psi"] + random.uniform(-0.1, 0.1), 1)
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
    service_manager.RegisterApplication(app.get_path(), {}, reply_handler=lambda: None, error_handler=lambda e: logging.error(f"GATT Error: {e}"))
    ad_manager.RegisterAdvertisement(ad.get_path(), {}, reply_handler=lambda: logging.info("BLE Advertising Active"), error_handler=lambda e: logging.error(f"Ad Error: {e}"))

    threading.Thread(target=mock_telemetry_loop, daemon=True).start()
    logging.info("Daemon ready for multi-vehicle telemetry")
    GLib.MainLoop().run()

if __name__ == "__main__": main()
