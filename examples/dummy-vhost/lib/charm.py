#!/usr/bin/env python3

from ops.charm import CharmBase, CharmEvents
from ops.framework import (
    Event,
    EventBase,
    StoredState,
)

from ops.model import ActiveStatus

from ops.main import main

import subprocess
import base64

from pathlib import Path


class DummyVhostReadyEvent(EventBase):
    pass


class DummyVhostCharmEvents(CharmEvents):
    vhost_ready = Event(DummyVhostReadyEvent)


class Charm(CharmBase):

    on = DummyVhostCharmEvents()

    document_root = Path('/var/www/dummy-vhost')
    index_file = document_root / 'index.html'
    index_template = 'templates/index.html'
    vhost_template = 'templates/dummy-vhost.conf'

    VHOST_PORT = 80

    state = StoredState()

    def __init__(self, *args):
        super().__init__(*args)

        try:
            self.state.ready
        except AttributeError:
            self.state.ready = False

        self.framework.observe(self.on.install, self)
        self.framework.observe(self.on.stop, self)

        self.framework.observe(self.on.vhost_config_relation_joined, self)
        self.framework.observe(self.on.vhost_ready, self)

    def on_install(self, event):
        log(f'on_install: Setting up dummy vhost files.')

        self.document_root.mkdir()

        with open(self.framework.charm_dir / self.index_template) as f:
            index_content = f.read()
        with open(self.index_file, 'w') as f:
            f.write(index_content)

        self.state.ready = True
        self.on.vhost_ready.emit()

    def on_stop(self, event):
        log(f'on_stop: removing dummy vhost files.')
        self.document_root.rmdir()

        self.state.ready = False

    def on_vhost_ready(self, event):
        self.framework.model.unit.status = ActiveStatus()

    def on_vhost_config_relation_joined(self, event):
        if not self.state.ready:
            event.defer()
            return

        with open(self.framework.charm_dir / self.vhost_template, 'rb') as f:
            vhost_content = base64.b64encode(f.read()).decode('utf-8')

        vhost_rdata = '- {' f'port: "{self.VHOST_PORT}", template: {vhost_content}' '}'
        event.relation.data[self.framework.model.unit]['vhosts'] = vhost_rdata


def log(message, level=None):
    """Write a message to the juju log"""
    command = ['juju-log']
    if level:
        command += ['-l', level]
    if not isinstance(message, str):
        message = repr(message)

    # https://elixir.bootlin.com/linux/latest/source/include/uapi/linux/binfmts.h
    # PAGE_SIZE * 32 = 4096 * 32
    MAX_ARG_STRLEN = 131072
    command += [message[:MAX_ARG_STRLEN]]
    # Missing juju-log should not cause failures in unit tests
    # Send log output to stderr
    subprocess.call(command)


if __name__ == '__main__':
    main(Charm)
