#!/usr/bin/env python3
"""Create / deploy the SMAGIC'26 PPE demo on ZEDEDA Cloud.

    export ZEDEDA_TOKEN=...            # API token (never commit it)
    ./deploy/zededa_deploy.py image    # register the container image
    ./deploy/zededa_deploy.py app      # create the edge app (marketplace entry)
    ./deploy/zededa_deploy.py deploy   # instance on the node, USB camera attached
    ./deploy/zededa_deploy.py status   # instance + USB assignment state
    ./deploy/zededa_deploy.py undeploy # delete the instance (image stays cached a while)

Defaults target the hummingbird cluster and sergey-cl260; override with flags.
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULTS = {
    'server': 'https://zedcontrol.hummingbird.zededa.net',
    'datastore': 'docker-hub',
    'image_name': 'ppe-stream_v6',
    'image_url': 'sergeyzededa/zdemo:6',
    'app_name': 'smagic26-ppe-detection',
    'device': 'smagic-ppe-demo',
    'instance': 'smagic-ppe-demo-ppe',
    'netinst': None,  # device default local network instance
    # Whole xHCI controller (PCI 00:14.0, buses 3/4, all USB-A ports). Per-port passthrough
    # (IO_TYPE_USB_DEVICE, e.g. PORTNO_1_USB_V2_0) goes through QEMU's emulated xHCI, which
    # breaks the camera's isochronous video transfers.
    'usb_port': 'USB1',
    'usb_type': 'IO_TYPE_USB_CONTROLLER',
    'stream_url': 'https://sspm.freeddns.org/videos/playlist.m3u8',
}

APP_PORT = 8000
CPUS = 4
MEMORY_KB = 3 * 1024 * 1024

VARIABLES = [
    ('CAMERA_STREAM_URL', 'Cloud stream URL (HLS/MP4/RTSP), "off" to disable', DEFAULTS['stream_url']),
    ('SOURCE_MODE', 'Start-up source: auto, stream, usb or local', 'auto'),
    ('USB_RESOLUTION', 'USB camera MJPEG resolution', '1280x720'),
    ('DEVICE_LABEL', 'Label shown under the title', 'OnLogic CL260'),
]


class Api:
    def __init__(self, server, token):
        self.base = server.rstrip('/') + '/api/v1'
        self.token = token

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        req.add_header('Authorization', f'Bearer {self.token}')
        req.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raw = e.read()
            if e.code != 404:
                sys.exit(f'{method} {path} -> HTTP {e.code}: {raw.decode()[:800]}')
            return None
        return json.loads(raw) if raw else {}

    def by_name(self, kind, name):
        return self.call('GET', f'/{kind}/name/{name}')


def template():
    lines = ['#cloud-config', 'runcmd:'] + [f'  - {k}=###{k}###' for k, _, _ in VARIABLES]
    return base64.b64encode('\n'.join(lines).encode()).decode()


def variable_group(values=None):
    values = values or {}
    return [{
        'name': 'Variable-Group-0', 'required': True, 'condition': None,
        'variables': [{
            'name': k, 'label': label, 'default': default, 'value': values.get(k, ''),
            'required': True, 'format': 'VARIABLE_FORMAT_TEXT', 'encode': 'FILE_ENCODING_UNSPECIFIED',
            'type': '', 'maxLength': '', 'processInput': '', 'options': [],
        } for k, label, default in VARIABLES],
    }]


def port_acls(instance=False):
    # App bundles and app instances spell the port-map target differently
    target = {'mapparams': {'port': APP_PORT}} if instance else {'portmapto': {'appPort': APP_PORT}}
    allow_all = {'matches': [{'type': 'ip', 'value': '0.0.0.0/0'}], 'actions': [], 'name': ''}
    portmap = {'matches': [{'type': 'protocol', 'value': 'tcp'}, {'type': 'lport', 'value': str(APP_PORT)},
                           {'type': 'ip', 'value': '0.0.0.0/0'}],
               'actions': [{'portmap': True, **target, 'drop': False, 'limit': False,
                            'limitrate': 0, 'limitunit': '', 'limitburst': 0}],
               'name': ''}
    return [allow_all, portmap]


def cmd_image(api, a):
    ds = api.by_name('datastores', a.datastore) or sys.exit(f'datastore {a.datastore} not found')
    img = api.by_name('apps/images', a.image_name)
    if not img:
        api.call('POST', '/apps/images', {
            'name': a.image_name, 'title': a.image_name, 'datastoreId': ds['id'],
            'imageRelUrl': a.image_url, 'imageFormat': 'CONTAINER', 'imageArch': 'AMD64',
            'imageType': 'IMAGE_TYPE_APPLICATION', 'description': 'SMAGIC26 PPE detection, cloud/USB/local sources',
        })
        print(f'image {a.image_name} -> {a.image_url} created')
        img = api.by_name('apps/images', a.image_name)
    if img['imageStatus'] != 'IMAGE_STATUS_READY':
        # A new record stays CREATED until it is uplinked to the datastore object
        img.update({'datastoreId': ds['id'], 'imageRelUrl': a.image_url})
        api.call('PUT', f'/apps/images/id/{img["id"]}/uplink', img)
        img = api.by_name('apps/images', a.image_name)
    print(f'image {a.image_name}: {img["imageStatus"]}')


def cmd_app(api, a):
    existing = api.by_name('apps', a.app_name)
    img = api.by_name('apps/images', a.image_name) or sys.exit(f'image {a.image_name} not found, run "image" first')
    ds = api.by_name('datastores', a.datastore)
    manifest = {
        'acKind': 'PodManifest', 'acVersion': '1.2.0', 'name': a.app_name, 'displayName': a.app_name,
        'deploymentType': 'DEPLOYMENT_TYPE_STAND_ALONE', 'vmmode': 'HV_PV', 'enablevnc': False, 'cpuPinningEnabled': False,
        'owner': {'user': 'Sergey Seperovich', 'company': 'ZEDEDA', 'website': 'www.zededa.com', 'email': 'sergey@zededa.com', 'group': ''},
        'desc': {'category': 'EdgeAI', 'appCategory': 'APP_CATEGORY_UNSPECIFIED',
                 'logo': {'logo': 'd345ecf4-9ab0-11f0-87b7-eed5b83106f4_logo'}},
        'images': [{'imagename': img['name'], 'imageid': img['id'], 'imageformat': 'CONTAINER', 'maxsize': '0',
                    'preserve': False, 'readonly': False, 'cleartext': True, 'ignorepurge': False, 'target': '',
                    'drvtype': '', 'mountpath': '', 'volumelabel': '', 'params': [],
                    'datastore': [{'id': ds['id'], 'name': ds['name']}] if ds else []}],
        'interfaces': [
            {'name': 'eth0', 'type': '', 'directattach': False, 'privateip': False, 'optional': False, 'acls': port_acls()},
            {'name': 'usbcam', 'type': a.usb_type, 'directattach': True, 'privateip': False, 'optional': True, 'acls': []},
        ],
        'resources': [{'name': 'resourceType', 'value': 'Custom'}, {'name': 'cpus', 'value': str(CPUS)},
                      {'name': 'memory', 'value': f'{MEMORY_KB:.2f}'}],
        'configuration': {'customConfig': {
            'name': 'custom', 'add': True, 'override': False, 'allowStorageResize': False, 'fieldDelimiter': '###',
            'template': template(), 'variableGroups': variable_group()}},
    }
    body = {
        'name': a.app_name, 'title': a.app_name, 'originType': 'ORIGIN_LOCAL',
        'description': 'YOLO11 PPE detection with live switching between a cloud stream, a USB camera and on-node video.',
        'userDefinedVersion': a.image_url.rsplit(':', 1)[-1], 'manifestJSON': manifest,
    }
    if existing:
        # PUT wants the full record, including its current revision
        body = {**existing, **body}
        api.call('PUT', f'/apps/id/{existing["id"]}', body)
        print(f'app {a.app_name} updated to {a.image_name}')
    else:
        api.call('POST', '/apps', body)
        print(f'app {a.app_name} created')


def cmd_deploy(api, a):
    if api.by_name('apps/instances', a.instance):
        print(f'instance {a.instance} already exists')
        return
    dev = api.by_name('devices', a.device) or sys.exit(f'device {a.device} not found')
    app = api.by_name('apps', a.app_name) or sys.exit(f'app {a.app_name} not found, run "app" first')
    if a.netinst:
        ni = api.by_name('netinsts', a.netinst)
    else:
        nis = api.call('GET', '/netinsts?next.pageSize=500')['list']
        ni = next((n for n in nis if n.get('deviceId') == dev['id'] and n.get('deviceDefault')), None)
    ni or sys.exit('no network instance found for the device')
    m = app['manifestJSON']
    img = m['images'][0]
    stream = a.stream_url or DEFAULTS['stream_url']
    body = {
        'name': a.instance, 'title': a.instance, 'appId': app['id'], 'deviceId': dev['id'],
        'projectId': dev['projectId'], 'appType': 'APP_TYPE_CONTAINER', 'activate': True,
        'vminfo': {'cpus': CPUS, 'memory': MEMORY_KB, 'mode': 'HV_PV', 'vnc': False},
        'manifestInfo': {'transitionAction': 'INSTANCE_TA_NONE'},
        'logs': {'access': True},
        'drives': [{'imagename': img['imagename'], 'maxsize': '0', 'preserve': False, 'readonly': False,
                    'drvtype': '', 'target': '', 'cleartext': True, 'mountpath': '', 'ignorepurge': False}],
        'interfaces': [
            {'intfname': 'eth0', 'intforder': 1, 'directattach': False, 'privateip': False,
             'netinstname': ni['name'], 'netinstid': ni['id'], 'acls': port_acls(instance=True), 'io': None},
        ],
        'customConfig': {
            'name': 'custom', 'add': True, 'override': False, 'allowStorageResize': False, 'fieldDelimiter': '###',
            'template': template(),
            'variableGroups': variable_group({'CAMERA_STREAM_URL': stream, 'SOURCE_MODE': 'auto',
                                              'USB_RESOLUTION': '1280x720',
                                              'DEVICE_LABEL': f'{a.device} · OnLogic CL260'})},
    }
    if a.usb_port:
        body['interfaces'].append({
            'intfname': 'usbcam', 'intforder': 2, 'directattach': True, 'privateip': False,
            'netinstname': '', 'acls': [], 'io': {'type': a.usb_type, 'name': a.usb_port, 'tags': {}}})
    api.call('POST', '/apps/instances', body)
    print(f'instance {a.instance} created on {a.device} (net {ni["name"]}, usb {a.usb_port or "none"})')


def cmd_status(api, a):
    inst = api.by_name('apps/instances', a.instance)
    if not inst:
        print(f'instance {a.instance} not found')
        return
    st = api.call('GET', f'/apps/instances/id/{inst["id"]}/status') or {}
    print(json.dumps({k: st.get(k) for k in ('name', 'runState', 'netStatusList', 'errInfo', 'swState')}, indent=1)[:3000])
    dev = api.by_name('devices', a.device)
    ds = api.call('GET', f'/devices/id/{dev["id"]}/status')
    for io in ds.get('ioStatusList', []):
        if a.usb_port in io.get('members', []):
            print(f'{a.usb_port}: assigned to {io.get("appName")}  err={io.get("err")}')


def cmd_undeploy(api, a):
    inst = api.by_name('apps/instances', a.instance)
    if not inst:
        print(f'instance {a.instance} not found')
        return
    api.call('DELETE', f'/apps/instances/id/{inst["id"]}')
    print(f'instance {a.instance} deleted')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('action', choices=['image', 'app', 'deploy', 'status', 'undeploy'])
    for k, v in DEFAULTS.items():
        p.add_argument('--' + k.replace('_', '-'), dest=k, default=v)
    a = p.parse_args()
    token = os.environ.get('ZEDEDA_TOKEN') or sys.exit('set ZEDEDA_TOKEN')
    api = Api(a.server, token)
    globals()['cmd_' + a.action](api, a)


if __name__ == '__main__':
    main()
