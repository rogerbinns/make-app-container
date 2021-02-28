.. contents::

What does it do?
================

This script creates a container, and then adds a control script in
~/.local/bin which auto-starts the container, runs an app inside, and
stops the container afterwards if not still in use.  It is like the
container is local host program.

You should also consider docker or podman.  This is a closer
alternative to tools like firejail.

Example
=======

This creates a container based on focal (Ubuntu 20.04 LTS) installing
the supplied chrome package inside.  It is only given access to your
screen (--gui) and your host Downloads directory is made available
inside.

    make-app-container.py create --gui focal --bind downloads \\ 
        --run google-chrome ~/containers/chrome-container \\
        google-chrome-stable_current_amd64.deb

To run, simply run the created control script which should be on your
$PATH.

   chrome-container --incognito www.example.com

There are flags to expose gui (X), 3d (mesa/dri), sound (pulseaudio),
webcams (v4l) etc, and bind host folders inside, such as ~/Downloads
or your Steam library.  You can also control what networking is
available - none, shared with the host, or being separate from the
host.

Benefits
========

The major benefit is the container shares nothing from the host,
except what you explicitly configure.  The control script means no
need to manually stop and start the containers.

The configuration is at the top of the control script which you can
edit.

Technical details
=================

This script is written in Python (3.6+).  The control script is
extracted from inside it.

The containers are created using debootstrap which creates a Debian or
Ubuntu root environment in a specified directory.  debootstrap is
available on most flavours of Linux.

This script creates a user of the same name and id inside the chroot
which keeps permissions consistent with files and directories provided
from the host.

The containers are run using systemd-nspawn and machinectl from
systemd.  The systemd-container package needs to exist inside which
means more recent versions of Debian/Ubuntu.

Unfortunately many operations involving the containers require root
access on the host.  If you configured as a gui application then
pkexec is used to get a graphical prompt.  Otherwise sudo is used.

Control Script
==============

The control script starts the container if not already running.  It
ensures appropriate files and directories are bound (eg for X,
pulseaudio, downloads, webcams etc).

Arguments supplied to the control script are then run inside the
container, appended to the --run argument if given at creation time.
Environment variables are appropriately set for X, pulseaudio, terminal
type etc.

Once that exits, the container is shutdown if no other instance of the
script is running.

Options
-------

The control script can take additional options.  These start with ++
to distinguish them from options going into the container, and must be
the first options supplied.

++show

    Shows the commands the script runs

++start / ++stop

    Only start or stop the container.  Do not run anything.  You can
    also use machinectl to stop the container.

++cmd

    Runs the remaining arguments as the command instead of what 
    was configured with --run

    You can use machinectl shell to get a shell inside the running
    container as root or user.

++network on | off | separate

    Overrides the network setting when starting the container

++aptupdate

    Starts the container with networking and performs apt to 
    download and install updates, then stops the container.

++aptupdateall

    Finds all control scripts in the same directory as this one
    (there is a marker) and runs ++aptupdate with each one

Private Gui
===========

Any running X application has continual full access to the screen (ie
can constantly record) as well as mouse movement and keyboard
activity.  (Fixing this was one of several motivations behind
Wayland.)

You can run a nested X environment as a window inside your existing
desktop. Install the package for Xephyr on your host.  It works well
enough but it not perfect.


Networking
==========

- always get private loopback

- on sharing explanation

- separate macvlan explanation
    bridge howto

Deeper Examples
===============

Steam
-----

Visual Studio Code
------------------

We are going to run this in a private window, with no access to the display, sound etc
using the default matchbox window manager.  Some dev packages are also installed.:

  make-app-container create --gui-private --bind gitconfig --packages python3-dev,python3-pip,build-essential groovy ~/containers/vscode ~/Downloads/code_amd64.deb

Now I can it with vscode.  Projects are bound into the container like this:

  sudo machinectl bind --mkdir vscode ~/projects/example

Emacs (text mode)
-----------------



IceWeasel
---------