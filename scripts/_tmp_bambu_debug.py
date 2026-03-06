import json
import os
import ssl
import time

import paho.mqtt.client as mqtt

cfg_list = json.loads(os.environ.get('BAMBU_PRINTERS_JSON', '[]'))
if not cfg_list:
    print('NO_PRINTER_CFG')
    raise SystemExit(1)

cfg = cfg_list[0]
holder = {'p': None}

def on_connect(client, _userdata, _flags, rc):
    if rc == 0:
        client.subscribe(f"device/{cfg['serial']}/report")

def on_message(_client, _userdata, msg):
    try:
        holder['p'] = json.loads(msg.payload.decode('utf-8', 'replace'))
    except Exception:
        holder['p'] = None

client = mqtt.Client(client_id='debug-once')
client.username_pw_set('bblp', cfg['access_code'])
client.on_connect = on_connect
client.on_message = on_message
client.tls_set(cert_reqs=ssl.CERT_NONE)
client.tls_insecure_set(True)
client.connect(cfg['host'], int(cfg.get('port', 8883)), keepalive=20)
client.loop_start()

start = time.time()
while time.time() - start < 12 and holder['p'] is None:
    time.sleep(0.2)

client.loop_stop()
client.disconnect()

payload = holder['p']
if not payload:
    print('NO_PAYLOAD')
    raise SystemExit(1)

report = payload.get('print', {}) if isinstance(payload, dict) else {}
print('TRAY_NOW', report.get('tray_now'))
print('VT_TRAY', report.get('vt_tray'))
print('AMS_TYPE', type(report.get('ams')).__name__)

ams = report.get('ams')
if isinstance(ams, list):
    for idx, ams_entry in enumerate(ams):
        if not isinstance(ams_entry, dict):
            continue
        trays = ams_entry.get('tray')
        print('AMS', idx, 'ID', ams_entry.get('id'), 'TRAY_LEN', len(trays) if isinstance(trays, list) else None)
        if isinstance(trays, list):
            for tray in trays[:8]:
                if isinstance(tray, dict):
                    picked = {k: tray.get(k) for k in ['id', 'slot', 'slot_id', 'tray_id', 'tray_sub_brands', 'tray_type', 'tray_color', 'material', 'brand', 'color']}
                    print('TRAY', picked)
elif isinstance(ams, dict):
    print('AMS_KEYS', list(ams.keys()))
    print('AMS_TRAY_MARKERS', {k: ams.get(k) for k in ['tray_now', 'tray_pre', 'tray_tar', 'vt_tray']})
    nested = ams.get('ams')
    print('AMS_NESTED_TYPE', type(nested).__name__)
    if isinstance(nested, list):
        print('AMS_NESTED_LEN', len(nested))
        for idx, ams_entry in enumerate(nested):
            if not isinstance(ams_entry, dict):
                continue
            trays = ams_entry.get('tray')
            print('AMS', idx, 'ID', ams_entry.get('id'), 'AMS_ID', ams_entry.get('ams_id'), 'TRAY_LEN', len(trays) if isinstance(trays, list) else None)
            print('AMS_UNIT_KEYS', sorted(list(ams_entry.keys())))
            print('AMS_NAME_LIKE', {k: ams_entry.get(k) for k in ['name', 'ams_name', 'nickname', 'label', 'sn', 'id']})
            if isinstance(trays, list):
                for tray in trays[:8]:
                    if isinstance(tray, dict):
                        picked = {k: tray.get(k) for k in ['id', 'slot', 'slot_id', 'tray_id', 'tray_sub_brands', 'tray_type', 'tray_color', 'material', 'brand', 'color']}
                        print('TRAY', picked)
