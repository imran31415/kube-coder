#!/usr/bin/env python3
"""A stand-in desktop notification server (org.freedesktop.Notifications) for
testing the desktop app on a headless Linux display (#833).

It logs every notification the app sends and, CLICK_AFTER seconds later,
"clicks" it: emits ActionInvoked(id, "default"), what a notification daemon
sends when the user clicks the notification body. Run it on the app's session
bus, e.g.:

    dbus-run-session -- bash -c 'fake-notifyd.py notify.log & exec kube-coder-desktop'
"""
import sys
import time

from gi.repository import Gio, GLib

LOG = open(sys.argv[1] if len(sys.argv) > 1 else '/dev/stdout', 'a', buffering=1)
CLICK_AFTER = 2

XML = """<node><interface name="org.freedesktop.Notifications">
<method name="GetCapabilities"><arg type="as" direction="out"/></method>
<method name="Notify"><arg type="s"/><arg type="u"/><arg type="s"/><arg type="s"/><arg type="s"/>
<arg type="as"/><arg type="a{sv}"/><arg type="i"/><arg type="u" direction="out"/></method>
<method name="CloseNotification"><arg type="u"/></method>
<method name="GetServerInformation"><arg type="s" direction="out"/><arg type="s" direction="out"/>
<arg type="s" direction="out"/><arg type="s" direction="out"/></method>
<signal name="ActionInvoked"><arg type="u"/><arg type="s"/></signal>
<signal name="NotificationClosed"><arg type="u"/><arg type="u"/></signal>
</interface></node>"""

node = Gio.DBusNodeInfo.new_for_xml(XML)
next_id = [1]


def click(conn, nid, sender):
    LOG.write(f'{time.strftime("%T")} click id={nid}\n')
    conn.emit_signal(sender, '/org/freedesktop/Notifications', 'org.freedesktop.Notifications',
                     'ActionInvoked', GLib.Variant('(us)', (nid, 'default')))
    conn.emit_signal(sender, '/org/freedesktop/Notifications', 'org.freedesktop.Notifications',
                     'NotificationClosed', GLib.Variant('(uu)', (nid, 2)))
    return False


def on_call(conn, sender, path, iface, method, params, inv):
    if method == 'GetCapabilities':
        inv.return_value(GLib.Variant('(as)', (['body', 'actions'],)))
    elif method == 'GetServerInformation':
        inv.return_value(GLib.Variant('(ssss)', ('fake-notifyd', 'kube-coder', '1', '1.2')))
    elif method == 'CloseNotification':
        inv.return_value(None)
    elif method == 'Notify':
        app, _replaces, _icon, summary, body, actions, _hints, _timeout = params.unpack()
        nid = next_id[0]
        next_id[0] += 1
        LOG.write(f'{time.strftime("%T")} notify id={nid} app={app!r} title={summary!r} '
                  f'body={body!r} actions={actions}\n')
        inv.return_value(GLib.Variant('(u)', (nid,)))
        GLib.timeout_add_seconds(CLICK_AFTER, click, conn, nid, sender)


def on_bus(conn, _name):
    conn.register_object('/org/freedesktop/Notifications', node.interfaces[0], on_call, None, None)


Gio.bus_own_name(Gio.BusType.SESSION, 'org.freedesktop.Notifications', Gio.BusNameOwnerFlags.NONE,
                 on_bus, None, None)
GLib.MainLoop().run()
