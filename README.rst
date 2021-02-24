What does it do?
================

This script creates a container, and then adds a control script in
~/.local/bin which auto-starts the container, runs an app inside, 
and stops the container afterwards.

Creation:

    make-app-container.py create --gui focal ~/containers/chrome-container \
        google-chrome-stable_current_amd64.deb

Running:

   chrome-container www.example.com

There are flags to expose gui (X), 3d (mesa/dri), sound (pulseaudio),
webcams (v4l) etc, and bind host folders inside, such as ~/Downloads
or your Steam library.

The benefit is the container shares nothing from the host, except what
you explicitly configure.  

Technical details
=================

systemd-nspawn - see docker/podman

inside user same id

root

Maintenance
===========

updateall