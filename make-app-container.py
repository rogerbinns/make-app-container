#!/usr/bin/env python3

# https://github.com/rogerbinns/make-app-container

import getpass
import hashlib
import json
import logging
import os
import pprint
import re
import shlex
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

# installed in container to redirect everything to pulseaudio
PULSE_SHIM = """
pcm.!default {
	type pulse
	fallback "sysdefault"
	hint { 
		show on 
		description "Default ALSA Output (PulseAudio Sound Server)" 
	} 
} 

ctl.!default {
	type pulse
	fallback "sysdefault"
}
"""

NETWORKD_CONF = """
[Match]
Name=mv-*

[Network]
DHCP=yes
"""


def run(options,
        cmd,
        *args,
        sudo=False,
        return_popen=False,
        wait_check=None,
        **kwargs):
    if sudo:
        cmd = [options.sudo] + cmd
    if wait_check:
        assert return_popen
    print(" >>> ", cmd)
    res = getattr(subprocess, "Popen" if return_popen else "run")(cmd, *args,
                                                                  **kwargs)
    if wait_check:
        try:
            rc = res.wait(timeout=wait_check)
            if rc != 0:
                sys.exit(f"   command failed with code { rc }")
        except subprocess.TimeoutExpired:
            pass

    return res


def create(options):
    options.folder = os.path.abspath(options.folder)

    if os.path.exists(options.folder):
        sys.exit(f"Directory already exists '{ options.folder }'")

    origname = os.path.basename(options.folder)

    name = re.sub(r"(^\.+)|[^a-zA-Z_\.-]+|(\.+$)", "", origname)
    name = re.sub(r"\.\.+", ".", name).strip()

    if name != origname:
        sys.exit(f"Name too complex - try { name } instead?")
    else:
        options.name = name

    if os.path.exists(opj(options.script_dir, options.name)):
        sys.exit(
            f"Script already exists { opj(options.script_dir, options.name) }")

    if options.add_password is not None:
        if options.add_password:
            if options.add_password[0] == "$":
                options.add_password = os.getenv(options.password[1:])
            elif options.add_password == "?":
                options.add_password = getpass.getpass(
                    f"Password for container user { options.add_user } > ")

    if not os.path.exists(options.deb_cache_dir):
        os.mkdir(options.deb_cache_dir)

    cmd = [
        "debootstrap", "--cache-dir=" + os.path.abspath(options.deb_cache_dir),
        "--components=main,universe"
    ]
    if options.arch:
        cmd.append(f"--arch={ options.arch }")

    if options.variant != "base":
        cmd.append(f"--variant={ options.variant }")

    # systemd-container brings in systemd which has systemd-networkd
    # sudo is used to invoke programs as user
    # gnupg is needed for 3rd party packages like chrome to add their repo keys
    pkgs = ["systemd-container", "adduser", "sudo", "gnupg"]

    if options.sound:
        pkgs.append("pulseaudio")

    if options.mesa:
        options.gui = True
        pkgs.extend(["mesa-vdpau-drivers"])

    if options.gui:
        # gets the libs etc as a side effect
        pkgs.extend(["xterm", "dbus-x11"])

    if options.gui_private and options.gui_private_window_manager == "matchbox-window-manager":
        pkgs.append("matchbox-window-manager")

    for p in options.packages or []:
        if p not in pkgs:
            pkgs.append(p)

    cmd.append("--include=" + ",".join(pkgs))

    cmd.append(options.distro)
    cmd.append(options.folder)

    # create the chroot
    res = run(options, cmd, sudo=True)
    if res.returncode != 0:
        sys.exit("debootstrap failed")

    # start the system
    cmd = ["systemd-nspawn", "-D", options.folder, "-b", "--console=read-only"]

    nspawn = run(options, cmd, sudo=True, return_popen=True, wait_check=5.0)

    with nspawn:

        def insidecmd(*args):
            return run(options, [
                "systemd-run", "--quiet", "--wait", "--pipe",
                f"--machine={ options.name }", *args
            ],
                       sudo=True)

        def copyin(source, dest):
            run(options, ["machinectl", "copy-to", options.name, source, dest],
                sudo=True)

        try:
            # in theory this isn't needed
            insidecmd("/usr/bin/hostnamectl", "set-hostname", options.name)
            # sudo whines when it can't find the hostname ip because it
            # always looks it up
            insidecmd("/bin/sh", "-c",
                      f"echo 127.0.1.1 { options.name } >> /etc/hosts")

            insidecmd("/usr/bin/systemctl", "enable",
                      "systemd-networkd.service")
            insidecmd("/usr/bin/systemctl", "start",
                      "systemd-networkd.service")

            # add a user
            insidecmd("/usr/sbin/adduser", "--uid", str(options.add_uid),
                      "--disabled-password", "--gecos", options.add_user,
                      options.add_user)

            groups = options.groups or []
            if options.gui or options.mesa or options.webcam:
                if "video" not in groups:
                    groups.append("video")
            for group in groups:
                insidecmd("/usr/sbin/adduser", options.add_user, group)

            if options.add_password:
                with tempfile.NamedTemporaryFile("wt",
                                                 prefix="mkcontainer") as tf:
                    tf.write(f"{ options.add_user }:{ options.add_password }")
                    tf.flush()
                    copyin(tf.name, "/tmp/chpasswd")
                insidecmd("/bin/sh", "-c",
                          "chpasswd < /tmp/chpasswd ; rm /tmp/chpasswd")

            # pulse shim
            with tempfile.NamedTemporaryFile("wt", prefix="mkcontainer") as tf:
                tf.write(PULSE_SHIM)
                tf.flush()
                copyin(tf.name, "/etc/asound.conf")

            # systemd-networkd macvlan config
            with tempfile.NamedTemporaryFile("wt", prefix="mkcontainer") as tf:
                fn = "/etc/systemd/network/make-app-container.network"
                tf.write(NETWORKD_CONF)
                tf.flush()
                insidecmd("/usr/bin/mkdir", "-p", "/etc/systemd/network")
                copyin(tf.name, fn)
                insidecmd("/usr/bin/chown", "root", fn)
                insidecmd("/usr/bin/chmod", "744", fn)

            # copy deb file across and install
            if options.debs:
                names = []
                for n in options.debs:
                    bn = os.path.basename(n)
                    names.append(f"/tmp/{ bn }")
                    copyin(n, names[-1])
                insidecmd("/usr/bin/apt", "install", "-y", *names)

        finally:
            run(options, ["machinectl", "stop", options.name], sudo=True)

    options.user = options.add_user
    genscript(options)


def makescript(options):
    options.folder = os.path.abspath(options.folder)
    # we may not have permissions to access the directory ...
    if os.path.isfile(options.folder):
        sys.exit(f"{ options.folder } is not a directory")
    options.name = os.path.basename(options.folder)
    genscript(options)


XEPHYR = [
    "Xephyr", "-resizeable", "-title", "%%TITLE%%", "-no-host-grab",
    "-host-cursor", ":%%NUM%%"
]


def genscript(options):
    if os.path.exists(opj(options.script_dir, options.name)):
        sys.exit(
            f"Script already exists { opj(options.script_dir, options.name) }")

    config = {
        "folder": options.folder,
        "name": options.name,
        "gui": options.gui,
        "gui-private": options.gui_private,
        "gui-private-start": XEPHYR,
        "gui-private-window-manager": options.gui_private_window_manager,
        "sound": options.sound,
        "mesa": options.mesa,
        "dbus": options.dbus,
        "network": options.network,
        "bind": options.bind,
        "run": options.run,
        "user": options.user,
        "webcam": options.webcam,
        "sudo": "sudo",
        "nspawn-extra-args": [],
    }

    op = []
    op.append(
        f"""#!{ "/usr/bin/pkexec" if options.gui else "/usr/bin/env" } python3

# Detailed doc at https://github.com/rogerbinns/make-app-container

# created with { shlex.join(sys.argv) }

# !MARKER FOR MAKE-APP-CONTAINER UPDATE!  # when run with ++aptupdateall this script will be included

# edit this to taste
config = { pprint.pformat(config, indent=4) }
""")

    save = False
    for line in open(sys.argv[0]):
        line = line.rstrip()
        if line.startswith("import "):
            op.append(line)
        if line.startswith("## CONTROL CODE START"):
            save = True
        elif line.startswith("## CONTROL CODE END"):
            save = False
        elif save:
            op.append(line)

    op.append(f"""
if __name__ == '__main__':
    controlcode(config) 
""")

    fn = opj(options.script_dir, options.name)

    with open(fn, "wt") as f:
        f.write("\n".join(op))
        os.chmod(f.name, 0o755)


## CONTROL CODE START
opj = os.path.join

BINDS = {
    "downloads": {
        "description": "Downloads folder",
        "folder": "~/Downloads"
    },
    "cache": {
        "description": "~/.cache",
        "folder": "~/.cache",
    },
    "steam": {
        "description": "steam library",
        "folder": "~/.local/share/Steam"
    },
    "gitconfig": {
        "description": "git config",
        "folder-ro": "~/.gitconfig"
    },
    "documents": {
        "description": "documents",
        "folder": "~/Documents"
    },
    "inputs": {
        "description":
        "input devices direct access (keyboards, mice, joysticks etc)",
        "folder": "/dev/input",
    }
}


def subrun(config: dict, cmd: list, return_popen=False, sudo=False, **kwargs):
    if sudo and os.getuid():
        cmd = [config["sudo"]] + cmd
    if config["show"]:
        print(">>>", shlex.join(cmd))
    return getattr(subprocess, "Popen" if return_popen else "run")(cmd,
                                                                   **kwargs)


def is_running(config):
    p = subrun(config, [
        "machinectl", "show", "--property", "State", "--value", config["name"]
    ],
               capture_output=True,
               text=True)
    return p.stdout.strip() == "running"


def expanduser(config, path):
    return path.replace("~", f"/home/{ config['user'] }")


def getuid():
    if os.getuid():
        return os.getuid()
    # we were invoked by pkexec or suid
    for n in "PKEXEC_UID", "SUDO_UID":
        if n in os.environ:
            return int(os.environ[n])
    # a good guess?
    return 1000


def start_xephyr(config):
    assert config["gui-private"]
    dir = "/tmp/.X11-unix"
    name = "mac-" + config["name"]
    # cleanup
    if os.path.islink(opj(dir, name)):
        os.remove(opj(dir, name))
    # find a free number
    num = 10 + (hashlib.sha1(config["name"].encode("utf8")).digest()[-1] % 89)
    while os.path.exists(opj(dir, "X" + str(num))):
        num += 1

    replacements = {
        "%%TITLE%%": f"{ config['name'] } (container)",
        "%%NUM%%": str(num),
    }

    def replace(s):
        for k, v in replacements.items():
            s = s.replace(k, v)
        return s

    cmd = [replace(c) for c in config["gui-private-start"]]

    if os.getuid() == 0:
        if "DISPLAY" not in os.environ:
            # started by pkexec, no $DISPLAY
            cmd = ["env", "DISPLAY=:0"] + cmd
        # we don't want to run xephyr as root!
        cmd = ["sudo", "-u", config["user"]] + cmd

    xephyr = subrun(config, cmd, return_popen=True)

    sockname = opj(dir, "X" + str(num))

    while not os.path.exists(sockname) and xephyr.returncode is None:
        time.sleep(0.5)

    if xephyr.returncode is not None:
        sys.exit(
            f"Private X server exited code { xephyr.returncode }: { shlex.join(cmd) }"
        )
    os.symlink("X" + str(num), opj(dir, name))
    if not os.getuid():
        os.chown(opj(dir, name), getuid(), -1, follow_symlinks=False)
    return num


def get_xephyr_displaynum(config):
    assert config["gui-private"]
    dir = "/tmp/.X11-unix"
    name = "mac-" + config["name"]

    try:
        link = os.readlink(opj(dir, name))
        assert link.startswith("X")
    except Exception:
        # probably already exited for other reasons
        return -1

    return int(link[1:])


def stop_xephyr(config):
    assert config["gui-private"]
    dir = "/tmp/.X11-unix"
    name = "mac-" + config["name"]
    num = get_xephyr_displaynum(config)
    try:
        os.remove(opj(dir, name))
    except Exception:
        pass
    if num < 0:
        return

    pidfile = f"/tmp/.X{ num }-lock"
    try:
        pid = int(open(pidfile, "rt").read().strip())
    except FileNotFoundError:
        return

    assert pid > 0
    os.kill(pid, signal.SIGTERM)


def start_container(config, for_update=False):
    cmd = [
        "systemd-nspawn",
        "-q",
        "-b",
        "-D",
        config["folder"],
        "--notify-ready=yes",
        "--console=passive",
    ]
    # binds
    if not for_update:
        for b in (config["bind"] or []):
            # although the key name is folder, files and paths all work
            arg = "bind"
            key = "folder"
            if "folder-ro" in BINDS[b]:
                arg = "bind-ro"
                key = "folder-ro"
            cmd.append(f"--{ arg }={ expanduser(config, BINDS[b][key]) }")
    # gui
    if not for_update and config["gui"]:
        if config["gui-private"]:
            num = start_xephyr(config)
        else:
            # DISPLAY is thrown away when the script is invoked via pkexec so
            # we assume :0 then
            num = os.environ.get("DISPLAY", ":0").split(":")[1].split(".")[0]
        cmd.append(f"--bind=/tmp/.X11-unix/X{ num }:/tmp/.X11-unix/X0")
        for p in "/usr/share/themes", "~/.themes":
            p = expanduser(config, p)
            if os.path.exists(p):
                cmd.append(f"--bind-ro={ p }")

    # mesa
    if not for_update and config["mesa"]:
        for name in "dri", "shm", "nvidia0", "nvidiactl", "nvidia-modeset":
            d = "/dev/" + name
            if os.path.exists(d):
                cmd.append("--bind=" + d)
    # sound
    if not for_update and config["sound"]:
        pulse = f"/run/user/{ getuid() }/pulse"
        assert os.path.exists(pulse)
        cmd.append(f"--bind={ pulse }:/run/user/host/pulse")
    # network
    if for_update or config["network"] == "on":
        # share host net
        pass
    elif config["network"] == "off":
        cmd.append("-p")
    elif config["network"] == "nat":
        cmd.append("--network-veth")
    else:
        assert config["network"] == "separate"
        for netif in json.loads(
                subrun(config, ["ip", "-o", "-j", "link", "show"],
                       capture_output=True).stdout):
            if "UP" in netif["flags"] and "LOOPBACK" not in netif[
                    "flags"] and "link_netnsid" not in netif:
                cmd.append(f"--network-macvlan={ netif['ifname'] }")
    # webcam
    if not for_update and config["webcam"] and os.path.exists("/dev/v4l"):
        cmd.append("--bind=/dev/v4l")
        byid = "/dev/v4l/by-id"
        for n in os.listdir(byid):
            if os.path.islink(opj(byid, n)):
                link = os.readlink(opj(byid, n))
                if not link.startswith("/"):
                    link = os.path.abspath(opj(byid, link))
                cmd.append("--bind=" + link)

    # dbus (makes tray icons etc work)
    if not for_update and config["dbus"]:
        bus = os.environ["DBUS_SESSION_BUS_ADDRESS"]
        assert bus.startswith("unix:path=")
        cmd.append(f"--bind={ bus.split('=', 1)[-1] }:/run/user/host/dbus")

    cmd.extend(config["nspawn-extra-args"])

    proc = subrun(config,
                  cmd,
                  return_popen=True,
                  sudo=True,
                  stdout=subprocess.DEVNULL)
    while True:
        # it is supposedly possible to figure out when the container is ready
        # but I can't work out how to actually do it.
        # https://github.com/systemd/systemd/issues/5620
        time.sleep(1)
        if proc.poll() is not None:
            sys.exit(f"Failed to start container (code { proc.returncode }")
        if is_running(config):
            break


def stop_container(config):
    cmd = ["machinectl", "stop", config["name"]]
    subrun(config, cmd, sudo=True)


def run_cmd(config, args, *, maintenance=False, **kwargs):
    # unfortunately we have to use sudo to become the user because
    # systemd-run won't let us start stuff as a non-root user inside
    # the container, while machinectl shell will but doesn't return
    # status etc
    cmd = [
        "systemd-run", "-M", config['name'], "-q",
        "--pipe" if config["gui"] else "--pty", "--wait", "--collect",
        "--send-sighup"
    ]
    if not maintenance:
        cmd.extend(("/usr/bin/sudo", "-u", config["user"]))
    cmd.append("/usr/bin/env")

    if config["gui"] and not maintenance:
        cmd.append("DISPLAY=unix/:0")
    if config["sound"] and not maintenance:
        cmd.append("PULSE_SERVER=unix:/run/user/host/pulse/native")
    if config["dbus"] and not maintenance:
        cmd.append("DBUS_SESSION_ADDRESS=unix:path=/run/user/host/dbus")
    if os.environ.get("TERM"):
        cmd.append(f"TERM={ os.environ['TERM'] }")
    cmd.extend(args)
    return subrun(config, cmd, sudo=True, **kwargs)


def controlcode(config, args=None):
    if args is None:
        args = sys.argv[1:]

    args = parse_args(config, args)
    config["show"] = args["show"]

    if args["aptupdate"] or args["aptupdateall"]:
        return do_apt_stuff(config, args)

    db = getdb(config)
    if not is_running(config):
        start_container(config)
        if config["gui-private"] and config["gui-private-window-manager"]:
            run_cmd(config, [config["gui-private-window-manager"]],
                    return_popen=True)
    if args["only"] == "start":
        return

    proc = None

    try:
        if not args["only"]:
            try:
                proc = run_cmd(config, args["cmd"])
            except KeyboardInterrupt:
                # fake the returncode
                class proc:
                    returncode = 2
    finally:
        # if we can get an exclusive lock then we are the only one using the
        # container and can shut it down
        try:
            shutdown = True
            db.cursor().execute("end")
            db.cursor().execute("begin exclusive")
        except Exception:
            shutdown = False

        if shutdown or args["only"] == "stop":
            if config["gui-private"]:
                stop_xephyr(config)
            stop_container(config)

        if proc is not None:
            sys.exit(proc.returncode)


def parse_args(config: dict, args: list):
    override_cmd = False
    res = {
        "only": None,
        "aptupdate": False,
        "aptupdateall": False,
        "show": False,
        "cmd": []
    }
    while args:
        if args[0] == "++aptupdate":
            res["aptupdate"] = True
            args.pop(0)
        elif args[0] == "++aptupdateall":
            res["aptupdateall"] = True
            args.pop(0)
        elif args[0] == "++cmd":
            override_cmd = True
            args.pop(0)
        elif args[0] == "++show":
            res["show"] = True
            args.pop(0)
        elif args[0] == "++start":
            res["only"] = "start"
            args.pop(0)
        elif args[0] == "++stop":
            res["only"] = "stop"
            args.pop(0)
        elif args[0] == "++network":
            args.pop(0)
            borken = None
            if not args:
                borken = "(No value)"
            else:
                net = args.pop(0)
                if net not in ("on", "off", "separate", "nat"):
                    borken = f"Unknown '{ net }'"
                else:
                    config["network"] = net
            if borken:
                sys.exit(
                    f"++network not understood.  Choose one of on, off, separate, nat. { borken }"
                )
        else:
            break

    res["cmd"] = args[:]
    if not override_cmd and config["run"]:
        res["cmd"] = [config["run"]] + args[:]

    # do we have something to do?
    if res["only"] or res["aptupdate"] or res["aptupdateall"] or res["cmd"]:
        return res
    sys.exit("Specify a command to run inside the container")


def getdb(config):
    p = f"/run/user/{ getuid() }/make-app-container/{ config['name'] }.lock"
    if not os.path.exists(os.path.dirname(p)):
        os.makedirs(os.path.dirname(p), exist_ok=True)
    db = sqlite3.connect(p, isolation_level=None)
    db.cursor().execute("begin")
    db.cursor().execute("select * from sqlite_master").fetchall()
    return db


def do_apt_stuff(config, args):
    if args["aptupdateall"]:
        mydir = os.path.abspath(os.path.dirname(sys.argv[0]))
        for f in sorted(os.listdir(mydir)):
            fn = opj(mydir, f)
            oneofus = False
            if os.path.isfile(fn):
                try:
                    for line in open(fn, "rt", encoding="iso-8859-1"):
                        if line.startswith(
                                "# !MARKER FOR MAKE-APP-CONTAINER UPDATE!"):
                            oneofus = True
                            break
                except Exception:
                    pass
            if oneofus:
                print("Update", fn)
                # this avoids pkexec
                subrun(config, [sys.executable, fn, "++aptupdate"])
                print()
    else:
        assert args["aptupdate"]
        if is_running(config):
            sys.exit(
                f"{ config['name'] } is already running - apt update skipped")
        start_container(config, for_update=True)
        run_cmd(config, ["apt", "update"], maintenance=True)
        run_cmd(config, ["apt", "upgrade"], maintenance=True)

        stop_container(config)


## CONTROL CODE END

if __name__ == "__main__":
    import argparse

    def add_script_args(p):
        p.add_argument(
            "--network",
            default="on",
            choices={"none", "on", "separate", "nat"},
            help=
            "none: only unshared loopback, on: share host networks, separate: use host networks to get own addresses, nat: own network behind this host but with internet access. [%(default)s]"
        )
        p.add_argument("--gui",
                       default=False,
                       action="store_true",
                       help="Enable gui (X) applications")
        p.add_argument(
            "--gui-private",
            default=False,
            action="store_true",
            help=
            "Run inside a private X window.  (All gui apps have complete access to the screen and inputs otherwise.)"
        )
        p.add_argument("--gui-private-window-manager",
                       default="matchbox-window-manager",
                       help="Window manager for private X [%(default)s]")
        p.add_argument("--mesa",
                       default=False,
                       action="store_true",
                       help="Enable 3d application")
        p.add_argument("--sound",
                       default=False,
                       action="store_true",
                       help="Enable sound application (pulseaudio)")
        p.add_argument("--webcam",
                       default=False,
                       action="store_true",
                       help="Expose webcam (video) devices")
        p.add_argument(
            "--dbus",
            default=False,
            action="store_true",
            help="Enable host dbus connection (eg makes tray icons work")
        p.add_argument("--bind",
                       default="",
                       help="One or more comma separated from " +
                       ", ".join(f"'{ k }': { v['description'] }"
                                 for k, v in BINDS.items()))
        p.add_argument(
            "--run",
            help="Command to run in the container [eg google-chrome or emacs]")
        p.add_argument(
            "--script-dir",
            default="~/.local/bin",
            help="Directory to create control script in [%(default)s]")

    parser = argparse.ArgumentParser(
        description=
        "Makes app containers https://github.com/rogerbinns/make-app-container"
    )
    parser.set_defaults(func=lambda *_: parser.error("Expected sub command"))

    parser.add_argument('--log-level',
                        default='INFO',
                        choices=('DEBUG', 'INFO', 'WARNING', 'ERROR',
                                 'CRITICAL'))

    parser.add_argument("--sudo",
                        default="sudo",
                        help="Command to sudo (pkexec is a gui alternative)")

    sub = parser.add_subparsers()

    p = sub.add_parser("create", help="Creates debian/ubuntu environment")
    p.set_defaults(func=create)
    p.add_argument("--arch", help="Override arch debootstrap picks")
    p.add_argument("--variant",
                   default="minbase",
                   choices={"base", "minbase", "buildd"},
                   help="debootstrap variant [%(default)s]")
    p.add_argument("--deb-cache-dir",
                   default=os.path.expanduser("~/.cache/make-app-container"),
                   help="Cache dir for debs [%(default)s]")
    p.add_argument("--add-user",
                   default=getpass.getuser(),
                   help="Username to add [%(default)s]")
    p.add_argument("--add-uid",
                   type=int,
                   default=os.getuid(),
                   help="Userid to add [%(default)s]")
    p.add_argument(
        "--add-password",
        help=
        "Password to set for the user.  If it starts with $ then treated as environment variable.  If ? then you are prompted.  Default is disabled password"
    )
    p.add_argument("--groups",
                   help="Comma separated list of groups to add the user to")
    p.add_argument(
        "--packages",
        help="Comma separated list of additional packages to install")

    add_script_args(p)

    p.add_argument(
        "distro",
        help="Debootstrap known distro. See /usr/share/debootstrap/scripts/")

    p.add_argument("folder", help="Folder for container")

    p.add_argument("debs",
                   nargs="*",
                   help="Additional .deb files to install inside")

    p = sub.add_parser("makescript", help="Creates control script")
    p.set_defaults(func=makescript)
    add_script_args(p)
    p.add_argument("--user",
                   default=getpass.getuser(),
                   help="Username to run as [%(default)s]")

    p.add_argument("folder", help="Folder for container")

    args = parser.parse_args()

    if hasattr(args, "bind"):
        args.bind = [a.strip()
                     for a in args.bind.split(",")] if args.bind else []
        for n in args.bind:
            if n not in BINDS:
                parser.error(f"Unknown bind '{ n }'")

    if getattr(args, "packages", None):
        args.packages = [a.strip() for a in args.packages.split(",")]

    if getattr(args, "groups", None):
        args.groups = [a.strip() for a in args.groups.split(",")]

    for n in "folder", "script_dir":
        if hasattr(args, n) and getattr(args, n):
            setattr(args, n, os.path.expanduser(getattr(args, n)))

    if hasattr(args, "gui_private"):
        args.gui = args.gui or args.gui_private
        if args.network == "on" and args.gui_private:
            parser.error("gui-private requires network setting other than on")

    if hasattr(args, "debs"):
        for deb in args.debs:
            if not os.path.isfile(deb):
                sys.exit(f"Deb file { deb } doesn't exist")

    logging.basicConfig(level=args.log_level,
                        format='%(levelname)s %(name)s %(message)s')
    logging.debug(f"{args=}")
    args.func(args)
