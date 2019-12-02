#!/usr/bin/python3

import os
import tempfile
import subprocess
import pathlib
import shutil
import unittest
import time

import op.model
import op.charm


# TODO: We need some manner of test to validate the actual ModelBackend implementation, round-tripped
# through the actual subprocess calls. Either this class could implement these functions as executables
# that were called via subprocess, or more simple tests that just test through ModelBackend while leaving
# these tests alone, depending on what proves easier.
class FakeModelBackend:
    def __init__(self):
        self.relation_set_calls = []

        self.unit_name = 'myapp/0'
        self.app_name = 'myapp'

    def relation_ids(self, relation_name):
        return {
            'db0': [],
            'db1': [4],
            'db2': [5, 6],
        }[relation_name]

    def relation_list(self, relation_id):
        try:
            return {
                4: ['remoteapp1/0'],
                5: ['remoteapp1/0'],
                6: ['remoteapp2/0'],
            }[relation_id]
        except KeyError:
            raise op.model.RelationNotFoundError()

    def relation_get(self, relation_id, member_name, app):
        try:
            return {
                4: {
                    'myapp/0': {'host': 'myapp-0'},
                    'remoteapp1/0': {'host': 'remoteapp1-0'},
                    'myapp': {'password': 'deadbeefcafe'},
                    'remoteapp1': {'secret': 'cafedeadbeef'}
                },
                5: {
                    'myapp/0': {'host': 'myapp-0'},
                    'remoteapp1/0': {'host': 'remoteapp1-0'},
                    'myapp': {'password': 'deadbeefcafe'},
                    'remoteapp1': {'secret': 'cafedeadbeef'}
                },
                6: {
                    'myapp/0': {'host': 'myapp-0'},
                    'remoteapp2/0': {'host': 'remoteapp2-0'},
                    'myapp': {'password': 'deadbeefcafe'},
                    'remoteapp1': {'secret': 'cafedeadbeef'}
                },
            }[relation_id][member_name]
        except KeyError:
            raise op.model.RelationNotFoundError()

    def relation_set(self, relation_id, key, value, is_app):
        if relation_id == 5:
            raise ValueError()
        self.relation_set_calls.append((relation_id, key, value, is_app))

    def config_get(self):
        return {
            'foo': 'foo',
            'bar': 1,
            'qux': True,
        }

class TestModel(unittest.TestCase):

    def setUp(self):
        self.backend = FakeModelBackend()
        meta = op.charm.CharmMeta()
        meta.relations = {'db0': None, 'db1': None, 'db2': None}
        self.model = op.model.Model('myapp/0', meta, self.backend)

        os.environ['JUJU_UNIT_NAME'] = 'myapp/0'
        self.addCleanup(os.environ.pop, 'JUJU_UNIT_NAME')

    def test_model(self):
        self.assertIs(self.model.app, self.model.unit.app)

    def test_relations_keys(self):
        for relation in self.model.relations['db2']:
            self.assertIn(self.model.unit, relation.data)
            unit_from_rel = next(filter(lambda u: u.name == 'myapp/0', relation.data.keys()))
            self.assertIs(self.model.unit, unit_from_rel)

    def test_get_relation(self):
        with self.assertRaises(op.model.ModelError):
            self.model.get_relation('db1', 'db1:4')
        db1_4 = self.model.get_relation('db1', 4)
        self.assertIsInstance(db1_4, op.model.Relation)
        dead_rel = self.model.get_relation('db1', 7)
        self.assertIsInstance(dead_rel, op.model.Relation)
        self.assertEqual(list(dead_rel.data.keys()), [self.model.unit, self.model.unit.app])
        self.assertEqual(dead_rel.data[self.model.unit], {})
        self.assertIsNone(self.model.get_relation('db0'))
        self.assertIs(self.model.get_relation('db1'), db1_4)
        with self.assertRaises(op.model.TooManyRelatedApps):
            self.model.get_relation('db2')

    def test_remote_units_is_our(self):
        for u in self.model.get_relation('db1').units:
            self.assertFalse(u._is_our_unit)
            self.assertFalse(u.app._is_our_app)

    def test_our_unit_is_our(self):
        self.assertTrue(self.model.unit._is_our_unit)
        self.assertTrue(self.model.unit.app._is_our_app)

    def test_unit_relation_data(self):
        random_unit = self.model._cache.get(op.model.Unit, 'randomunit/0')
        with self.assertRaises(KeyError):
            self.model.get_relation('db1').data[random_unit]
        remoteapp1_0 = next(filter(lambda u: u.name == 'remoteapp1/0', self.model.get_relation('db1').units))
        self.assertEqual(self.model.get_relation('db1').data[remoteapp1_0], {'host': 'remoteapp1-0'})

    def test_remote_app_relation_data(self):
        self.backend = op.model.ModelBackend()
        meta = op.charm.CharmMeta()
        meta.relations = {'db0': None, 'db1': None, 'db2': None}
        self.model = op.model.Model('myapp/0', meta, self.backend)

        fake_script(self, 'relation-ids', """[ "$1" = db1 ] && echo '["db1:4"]' || echo '[]'""")
        fake_script(self, 'relation-list', """[ "$2" = 4 ] && echo '["remoteapp1/0", "remoteapp1/1"]' || exit 2""")
        fake_script(self, 'relation-get', """[ "$2" = 4 ] && [ "$4" = remoteapp1 ] && echo '{"secret": "cafedeadbeef"}' || exit 2""")

        # Try to get relation data for an invalid remote application.
        random_app = self.model._cache.get(op.model.Application, 'randomapp')
        with self.assertRaises(KeyError):
            self.model.get_relation('db1').data[random_app]

        remoteapp1 = self.model.get_relation('db1').app
        self.assertEqual(self.model.get_relation('db1').data[remoteapp1], {'secret': 'cafedeadbeef'})

        self.assertEqual(fake_script_calls(self), [
            ['relation-ids', 'db1', '--format=json'],
            ['relation-list', '-r', '4', '--format=json'],
            ['relation-get', '-r', '4', '-', 'remoteapp1', '--app=True', '--format=json'],
        ])

    def test_relation_data_modify_remote(self):
        rel_db1 = self.model.get_relation('db1')
        remoteapp1_0 = next(filter(lambda u: u.name == 'remoteapp1/0', self.model.get_relation('db1').units))
        # Force memory cache to be loaded.
        self.assertIn('host', rel_db1.data[remoteapp1_0])
        with self.assertRaises(op.model.RelationDataError):
            rel_db1.data[remoteapp1_0]['foo'] = 'bar'
        self.assertEqual(self.backend.relation_set_calls, [])
        self.assertNotIn('foo', rel_db1.data[remoteapp1_0])

    def test_relation_data_modify_our(self):
        rel_db1 = self.model.get_relation('db1')
        # Force memory cache to be loaded.
        self.assertIn('host', rel_db1.data[self.model.unit])
        rel_db1.data[self.model.unit]['host'] = 'bar'
        self.assertEqual(self.backend.relation_set_calls, [(4, 'host', 'bar', False)])
        self.assertEqual(rel_db1.data[self.model.unit]['host'], 'bar')

    def test_app_relation_data_modify_local_as_leader(self):
        self.backend = op.model.ModelBackend()
        meta = op.charm.CharmMeta()
        meta.relations = {'db0': None, 'db1': None, 'db2': None}
        self.model = op.model.Model('myapp/0', meta, self.backend)

        fake_script(self, 'relation-ids', """[ "$1" = db1 ] && echo '["db1:4"]' || echo '[]'""")
        fake_script(self, 'relation-list', """[ "$2" = 4 ] && echo '["remoteapp1/0", "remoteapp1/1"]' || exit 2""")
        fake_script(self, 'relation-get', """[ "$2" = 4 ] && [ "$4" = myapp ] && echo '{"password": "deadbeefcafe"}' || exit 2""")
        fake_script(self, 'relation-set', """[ "$2" = 4 ] && exit 0 || exit 2""")
        fake_script(self, 'is-leader', 'echo true')

        local_app = self.model.unit.app

        rel_db1 = self.model.get_relation('db1')
        self.assertEqual(rel_db1.data[local_app], {'password': 'deadbeefcafe'})

        rel_db1.data[local_app]['password'] = 'foo'

        self.assertEqual(rel_db1.data[local_app]['password'], 'foo')

        self.assertEqual(fake_script_calls(self), [
            ['relation-ids', 'db1', '--format=json'],
            ['relation-list', '-r', '4', '--format=json'],
            ['relation-get', '-r', '4', '-', 'myapp', '--app=True', '--format=json'],
            ['is-leader', '--format=json'],
            ['relation-set', '-r', '4', 'password=foo', '--app=True'],
        ])

    def test_app_relation_data_modify_local_as_minion(self):
        self.backend = op.model.ModelBackend()
        meta = op.charm.CharmMeta()
        meta.relations = {'db0': None, 'db1': None, 'db2': None}
        self.model = op.model.Model('myapp/0', meta, self.backend)

        fake_script(self, 'relation-ids', """[ "$1" = db1 ] && echo '["db1:4"]' || echo '[]'""")
        fake_script(self, 'relation-list', """[ "$2" = 4 ] && echo '["remoteapp1/0", "remoteapp1/1"]' || exit 2""")
        fake_script(self, 'relation-get', """[ "$2" = 4 ] && [ "$4" = myapp ] && echo '{"password": "deadbeefcafe"}' || exit 2""")
        fake_script(self, 'is-leader', 'echo false')

        local_app = self.model.unit.app

        rel_db1 = self.model.get_relation('db1')
        self.assertEqual(rel_db1.data[local_app], {'password': 'deadbeefcafe'})

        with self.assertRaises(op.model.RelationDataError):
            rel_db1.data[local_app]['password'] = 'foobar'

        self.assertEqual(fake_script_calls(self), [
            ['relation-ids', 'db1', '--format=json'],
            ['relation-list', '-r', '4', '--format=json'],
            ['relation-get', '-r', '4', '-', 'myapp', '--app=True', '--format=json'],
            ['is-leader', '--format=json'],
        ])

    def test_relation_data_del_key(self):
        rel_db1 = self.model.get_relation('db1')
        # Force memory cache to be loaded.
        self.assertIn('host', rel_db1.data[self.model.unit])
        del rel_db1.data[self.model.unit]['host']
        self.assertEqual(self.backend.relation_set_calls, [(4, 'host', '', False)])
        self.assertNotIn('host', rel_db1.data[self.model.unit])

    def test_relation_set_fail(self):
        rel_db2 = self.model.relations['db2'][0]
        # Force memory cache to be loaded.
        self.assertIn('host', rel_db2.data[self.model.unit])
        with self.assertRaises(ValueError):
            rel_db2.data[self.model.unit]['host'] = 'bar'
        self.assertEqual(rel_db2.data[self.model.unit]['host'], 'myapp-0')
        with self.assertRaises(ValueError):
            del rel_db2.data[self.model.unit]['host']
        self.assertIn('host', rel_db2.data[self.model.unit])

    def test_relation_get_set_is_app_arg(self):
        self.backend = op.model.ModelBackend()

        # No is_app provided.
        with self.assertRaises(TypeError):
            self.backend.relation_set(1, 'fookey', 'barval')

        with self.assertRaises(TypeError):
            self.backend.relation_get(1, 'fooentity')

        # Invalid types for is_app.
        for is_app_v in [None, 1, 2.0, 'a', b'beef']:
            with self.assertRaises(RuntimeError):
                self.backend.relation_set(1, 'fookey', 'barval', is_app=is_app_v)

            with self.assertRaises(RuntimeError):
                self.backend.relation_get(1, 'fooentity', is_app=is_app_v)

    def test_relation_data_type_check(self):
        rel_db1 = self.model.get_relation('db1')
        with self.assertRaises(op.model.RelationDataError):
            rel_db1.data[self.model.unit]['foo'] = 1
        with self.assertRaises(op.model.RelationDataError):
            rel_db1.data[self.model.unit]['foo'] = {'foo': 'bar'}
        with self.assertRaises(op.model.RelationDataError):
            rel_db1.data[self.model.unit]['foo'] = None
        self.assertEqual(self.backend.relation_set_calls, [])

    def test_config(self):
        self.assertEqual(self.model.config, {
            'foo': 'foo',
            'bar': 1,
            'qux': True,
        })
        with self.assertRaises(TypeError):
            # Confirm that we cannot modify config values.
            self.model.config['foo'] = 'bar'

    def test_is_leader(self):
        self.backend = op.model.ModelBackend()
        meta = op.charm.CharmMeta()
        meta.relations = {'db0': None, 'db1': None, 'db2': None}
        self.model = op.model.Model('myapp/0', meta, self.backend)

        def check_remote_units():
            fake_script(self, 'relation-ids',
                        """[ "$1" = db1 ] && echo '["db1:4"]' || echo '[]'""")

            fake_script(self, 'relation-list',
                        """[ "$2" = 4 ] && echo '["remoteapp1/0", "remoteapp1/1"]' || exit 2""")

            # Cannot determine leadership for remote units.
            for u in self.model.get_relation('db1').units:
                with self.assertRaises(RuntimeError):
                    u.is_leader()

        fake_script(self, 'is-leader', 'echo true')
        self.assertTrue(self.model.unit.is_leader())

        check_remote_units()

        self.backend = op.model.ModelBackend()
        self.model = op.model.Model('myapp/0', meta, self.backend)

        fake_script(self, 'is-leader', 'echo false')
        self.assertFalse(self.model.unit.is_leader())

        check_remote_units()

        self.assertEqual(fake_script_calls(self), [
            ['is-leader', '--format=json'],
            ['relation-ids', 'db1', '--format=json'],
            ['relation-list', '-r', '4', '--format=json'],
            ['is-leader', '--format=json'],
            ['relation-ids', 'db1', '--format=json'],
            ['relation-list', '-r', '4', '--format=json'],
        ])

    def test_is_leader_refresh(self):
        self.backend = op.model.ModelBackend()
        meta = op.charm.CharmMeta()
        meta.relations = {'db0': None, 'db1': None, 'db2': None}
        self.model = op.model.Model('myapp/0', meta, self.backend)

        # A sanity check.
        self.assertGreater(time.monotonic(), op.model.ModelBackend.LEASE_RENEWAL_PERIOD.total_seconds())

        fake_script(self, 'is-leader', 'echo false')
        self.assertFalse(self.model.unit.is_leader())

        # Change the leadership status and force a recheck.
        fake_script(self, 'is-leader', 'echo true')
        self.backend._leader_check_time = 0
        self.assertTrue(self.model.unit.is_leader())

        # Force a recheck without changing the leadership status.
        fake_script(self, 'is-leader', 'echo true')
        self.backend._leader_check_time = 0
        self.assertTrue(self.model.unit.is_leader())

    def test_resources(self):
        backend = op.model.ModelBackend()
        meta = op.charm.CharmMeta()
        meta.resources = {'foo': None, 'bar': None}
        model = op.model.Model('myapp/0', meta, backend)

        with self.assertRaises(RuntimeError):
            model.resources.fetch('qux')

        fake_script(self, 'resource-get', 'exit 1')
        with self.assertRaises(op.model.ModelError):
            model.resources.fetch('foo')

        fake_script(self, 'resource-get', 'echo /var/lib/juju/agents/unit-test-0/resources/$1/$1.tgz')
        self.assertEqual(model.resources.fetch('foo').name, 'foo.tgz')
        self.assertEqual(model.resources.fetch('bar').name, 'bar.tgz')

    def test_pod_spec(self):
        meta = op.charm.CharmMeta()
        meta.relations = {'db0': None, 'db1': None, 'db2': None}
        model = op.model.Model('myapp/0', meta, op.model.ModelBackend())

        fake_script(self, 'pod-spec-set', """
                    cat $2 > $(dirname $0)/spec.json
                    [[ -n $4 ]] && cat $4 > $(dirname $0)/k8s_res.json || true
                    """)
        spec_path = self.fake_script_path / 'spec.json'
        k8s_res_path = self.fake_script_path / 'k8s_res.json'

        model.pod.set_spec({'foo': 'bar'})
        self.assertEqual(spec_path.read_text(), '{"foo": "bar"}')
        self.assertFalse(k8s_res_path.exists())

        model.pod.set_spec({'bar': 'foo'}, {'qux': 'baz'})
        self.assertEqual(spec_path.read_text(), '{"bar": "foo"}')
        self.assertEqual(k8s_res_path.read_text(), '{"qux": "baz"}')

    def test_base_status_instance_raises(self):
        with self.assertRaises(TypeError):
            op.model.StatusBase('test')

    def test_active_message_raises(self):
        with self.assertRaises(TypeError):
            op.model.ActiveStatus('test')

    def test_local_set_valid_unit_status(self):
        self.backend = op.model.ModelBackend()
        meta = op.charm.CharmMeta()
        meta.relations = {'db0': None, 'db1': None, 'db2': None}
        self.model = op.model.Model('myapp/0', meta, self.backend)

        test_cases = [(
            op.model.ActiveStatus(),
            lambda: fake_script(self, 'status-set', 'exit 0'),
            lambda: self.assertEqual(fake_script_calls(self, True), [['status-set', '--application=False', 'active', '']]),
        ), (
            op.model.MaintenanceStatus('Yellow'),
            lambda: fake_script(self, 'status-set', 'exit 0'),
            lambda: self.assertEqual(fake_script_calls(self, True), [['status-set', '--application=False', 'maintenance', 'Yellow']]),
        ), (
            op.model.BlockedStatus('Red'),
            lambda: fake_script(self, 'status-set', 'exit 0'),
            lambda: self.assertEqual(fake_script_calls(self, True), [['status-set', '--application=False', 'blocked', 'Red']]),
        ), (
            op.model.WaitingStatus('White'),
            lambda: fake_script(self, 'status-set', 'exit 0'),
            lambda: self.assertEqual(fake_script_calls(self, True), [['status-set', '--application=False', 'waiting', 'White']]),
        )]

        for target_status, setup_tools, check_tool_calls in test_cases:
            setup_tools()

            self.model.unit.status = target_status

            self.assertEqual(self.model.unit.status, target_status)

            check_tool_calls()

    def test_local_set_valid_app_status(self):
        self.backend = op.model.ModelBackend()
        meta = op.charm.CharmMeta()
        meta.relations = {'db0': None, 'db1': None, 'db2': None}
        self.model = op.model.Model('myapp/0', meta, self.backend)

        fake_script(self, 'is-leader', 'echo true')

        test_cases = [(
            op.model.ActiveStatus(),
            lambda: fake_script(self, 'status-set', 'exit 0'),
            lambda: self.assertIn(['status-set', '--application=True', 'active', ''], fake_script_calls(self, True)),
        ), (
            op.model.MaintenanceStatus('Yellow'),
            lambda: fake_script(self, 'status-set', 'exit 0'),
            lambda: self.assertIn(['status-set', '--application=True', 'maintenance', 'Yellow'], fake_script_calls(self, True)),
        ), (
            op.model.BlockedStatus('Red'),
            lambda: fake_script(self, 'status-set', 'exit 0'),
            lambda: self.assertIn(['status-set', '--application=True', 'blocked', 'Red'], fake_script_calls(self, True)),
        ), (
            op.model.WaitingStatus('White'),
            lambda: fake_script(self, 'status-set', 'exit 0'),
            lambda: self.assertIn(['status-set', '--application=True', 'waiting', 'White'], fake_script_calls(self, True)),
        )]

        for target_status, setup_tools, check_tool_calls in test_cases:
            setup_tools()

            self.model.app.status = target_status

            self.assertEqual(self.model.app.status, target_status)

            check_tool_calls()

    def test_set_app_status_non_leader_raises(self):
        self.backend = op.model.ModelBackend()
        meta = op.charm.CharmMeta()
        meta.relations = {'db0': None, 'db1': None, 'db2': None}
        self.model = op.model.Model('myapp/0', meta, self.backend)

        fake_script(self, 'is-leader', 'echo false')

        with self.assertRaises(RuntimeError):
            self.model.app.status

        with self.assertRaises(RuntimeError):
            self.model.app.status = op.model.ActiveStatus()

    def test_local_set_invalid_status(self):
        self.backend = op.model.ModelBackend()
        meta = op.charm.CharmMeta()
        meta.relations = {'db0': None, 'db1': None, 'db2': None}
        self.model = op.model.Model('myapp/0', meta, self.backend)

        fake_script(self, 'status-set', 'exit 1')
        fake_script(self, 'is-leader', 'echo true')

        with self.assertRaises(op.model.ModelError):
            self.model.unit.status = op.model.UnknownStatus()

        self.assertEqual(fake_script_calls(self, True), [
            ['status-set', '--application=False', 'unknown', ''],
        ])

        with self.assertRaises(op.model.ModelError):
            self.model.app.status = op.model.UnknownStatus()

        # A leadership check is needed for application status.
        self.assertEqual(fake_script_calls(self, True), [
            ['is-leader', '--format=json'],
            ['status-set', '--application=True', 'unknown', ''],
        ])

    def test_status_set_is_app_not_bool_raises(self):
        self.backend = op.model.ModelBackend()

        for is_app_v in [None, 1, 2.0, 'a', b'beef', object]:
            with self.assertRaises(RuntimeError):
                self.backend.status_set(op.model.ActiveStatus, is_app=is_app_v)

    def test_remote_unit_status(self):
        self.backend = op.model.ModelBackend()
        meta = op.charm.CharmMeta()
        meta.relations = {'db0': None, 'db1': None, 'db2': None}
        self.model = op.model.Model('myapp/0', meta, self.backend)

        fake_script(self, 'relation-ids', """[ "$1" = db1 ] && echo '["db1:4"]' || echo '[]'""")
        fake_script(self, 'relation-list', """[ "$2" = 4 ] && echo '["remoteapp1/0", "remoteapp1/1"]' || exit 2""")

        remote_unit = next(filter(lambda u: u.name == 'remoteapp1/0', self.model.get_relation('db1').units))

        test_statuses = (
            op.model.UnknownStatus(),
            op.model.ActiveStatus(),
            op.model.MaintenanceStatus('Yellow'),
            op.model.BlockedStatus('Red'),
            op.model.WaitingStatus('White'),
        )

        for target_status in test_statuses:
            with self.assertRaises(RuntimeError):
                remote_unit.status = target_status

    def test_remote_app_status(self):
        remoteapp1 = self.model.get_relation('db1').app

        # Remote application status is always unknown.
        self.assertIsInstance(remoteapp1.status, op.model.UnknownStatus)

        test_statuses = (
            op.model.UnknownStatus(),
            op.model.ActiveStatus(),
            op.model.MaintenanceStatus('Upgrading software'),
            op.model.BlockedStatus('Awaiting manual resolution'),
            op.model.WaitingStatus('Awaiting related app updates'),
        )
        for target_status in test_statuses:
            with self.assertRaises(RuntimeError):
                remoteapp1.status = target_status


class TestModelBackend(unittest.TestCase):

    def setUp(self):
        os.environ['JUJU_UNIT_NAME'] = 'myapp/0'
        self.addCleanup(os.environ.pop, 'JUJU_UNIT_NAME')

        self.backend = op.model.ModelBackend()

    def test_relation_tool_errors(self):
        RELATION_ERROR_MESSAGE = "ERROR invalid value \"$1\" for option -r: relation not found"

        test_cases = [(
            lambda: fake_script(self, 'relation-list', f'echo fooerror >&2 ; exit 1'),
            lambda: self.backend.relation_list(3),
            op.model.ModelError,
            [['relation-list', '-r', '3', '--format=json']],
        ), (
            lambda: fake_script(self, 'relation-list', f'echo {RELATION_ERROR_MESSAGE} >&2 ; exit 2'),
            lambda: self.backend.relation_list(3),
            op.model.RelationNotFoundError,
            [['relation-list', '-r', '3', '--format=json']],
        ), (
            lambda: fake_script(self, 'relation-set', f'echo fooerror >&2 ; exit 1'),
            lambda: self.backend.relation_set(3, 'foo', 'bar', is_app=False),
            op.model.ModelError,
            [['relation-set', '-r', '3', 'foo=bar', '--app=False']],
        ), (
            lambda: fake_script(self, 'relation-set', f'echo {RELATION_ERROR_MESSAGE} >&2 ; exit 2'),
            lambda: self.backend.relation_set(3, 'foo', 'bar', is_app=False),
            op.model.RelationNotFoundError,
            [['relation-set', '-r', '3', 'foo=bar', '--app=False']],
        ), (
            lambda: fake_script(self, 'relation-get', f'echo fooerror >&2 ; exit 1'),
            lambda: self.backend.relation_get(3, 'remote/0', is_app=False),
            op.model.ModelError,
            [['relation-get', '-r', '3', '-', 'remote/0', '--app=False', '--format=json']],
        ), (
            lambda: fake_script(self, 'relation-get', f'echo {RELATION_ERROR_MESSAGE} >&2 ; exit 2'),
            lambda: self.backend.relation_get(3, 'remote/0', is_app=False),
            op.model.RelationNotFoundError,
            [['relation-get', '-r', '3', '-', 'remote/0', '--app=False', '--format=json']],
        )]

        for do_fake, run, exception, calls in test_cases:
            do_fake()
            with self.assertRaises(exception):
                run()
            self.assertEqual(fake_script_calls(self, clear=True), calls)

    def test_status_is_app_forced_kwargs(self):
        fake_script(self, 'status-get', 'exit 1')
        fake_script(self, 'status-set', 'exit 1')

        test_cases = (
            lambda: self.backend.status_get(False),
            lambda: self.backend.status_get(True),
            lambda: self.backend.status_set('active', '', False),
            lambda: self.backend.status_set('active', '', True),
        )

        for case in test_cases:
            with self.assertRaises(TypeError):
                case()


class TestModelBackend(unittest.TestCase):

    def setUp(self):
        os.environ['JUJU_UNIT_NAME'] = 'myapp/0'
        self.addCleanup(os.environ.pop, 'JUJU_UNIT_NAME')

        self.backend = op.model.ModelBackend()

    def test_relation_tool_errors(self):
        RELATION_ERROR_MESSAGE = "ERROR invalid value \"$1\" for option -r: relation not found"

        test_cases = [(
            lambda: fake_script(self, 'relation-list', f'echo fooerror >&2 ; exit 1'),
            lambda: self.backend.relation_list(3),
            op.model.ModelError,
            [['relation-list', '-r', '3', '--format=json']],
        ), (
            lambda: fake_script(self, 'relation-list', f'echo {RELATION_ERROR_MESSAGE} >&2 ; exit 2'),
            lambda: self.backend.relation_list(3),
            op.model.RelationNotFoundError,
            [['relation-list', '-r', '3', '--format=json']],
        ), (
            lambda: fake_script(self, 'relation-set', f'echo fooerror >&2 ; exit 1'),
            lambda: self.backend.relation_set(3, 'foo', 'bar'),
            op.model.ModelError,
            [['relation-set', '-r', '3', 'foo=bar']],
        ), (
            lambda: fake_script(self, 'relation-set', f'echo {RELATION_ERROR_MESSAGE} >&2 ; exit 2'),
            lambda: self.backend.relation_set(3, 'foo', 'bar'),
            op.model.RelationNotFoundError,
            [['relation-set', '-r', '3', 'foo=bar']],
        ), (
            lambda: fake_script(self, 'relation-get', f'echo fooerror >&2 ; exit 1'),
            lambda: self.backend.relation_get(3, 'remote/0'),
            op.model.ModelError,
            [['relation-get', '-r', '3', '-', 'remote/0', '--format=json']],
        ), (
            lambda: fake_script(self, 'relation-get', f'echo {RELATION_ERROR_MESSAGE} >&2 ; exit 2'),
            lambda: self.backend.relation_get(3, 'remote/0'),
            op.model.RelationNotFoundError,
            [['relation-get', '-r', '3', '-', 'remote/0', '--format=json']],
        )]

        for do_fake, run, exception, calls in test_cases:
            do_fake()
            with self.assertRaises(exception):
                run()
            self.assertEqual(fake_script_calls(self, clear=True), calls)


def fake_script(test_case, name, content):
    if not hasattr(test_case, 'fake_script_path'):
        fake_script_path = tempfile.mkdtemp('-fake_script')
        os.environ['PATH'] = f'{fake_script_path}:{os.environ["PATH"]}'

        def cleanup():
            shutil.rmtree(fake_script_path)
            os.environ['PATH'] = os.environ['PATH'].replace(fake_script_path + ':', '')

        test_case.addCleanup(cleanup)
        test_case.fake_script_path = pathlib.Path(fake_script_path)

    with open(test_case.fake_script_path / name, "w") as f:
        # Before executing the provided script, dump the provided arguments in calls.txt.
        f.write('#!/bin/bash\n{ echo -n $(basename $0); for s in "$@"; do echo -n \\;$s; done; echo; } >> $(dirname $0)/calls.txt\n' + content)
    os.chmod(test_case.fake_script_path / name, 0o755)

def fake_script_calls(test_case, clear=False):
    with open(test_case.fake_script_path / 'calls.txt', 'r+') as f:
        calls = [line.split(';') for line in f.read().splitlines()]
        if clear:
            f.truncate(0)
        return calls


class FakeScriptTest(unittest.TestCase):

    def test_fake_script_works(self):
        fake_script(self, 'foo', 'echo foo runs')
        fake_script(self, 'bar', 'echo bar runs')
        output = subprocess.getoutput('foo a "b c"; bar "d e" f')
        self.assertEqual(output, 'foo runs\nbar runs')
        self.assertEqual(fake_script_calls(self), [
            ['foo', 'a', 'b c'],
            ['bar', 'd e', 'f'],
        ])

    def test_fake_script_clear(self):
        fake_script(self, 'foo', 'echo foo runs')

        output = subprocess.getoutput('foo a "b c"')
        self.assertEqual(output, 'foo runs')

        self.assertEqual(fake_script_calls(self, clear=True), [['foo', 'a', 'b c']])

        fake_script(self, 'bar', 'echo bar runs')

        output = subprocess.getoutput('bar "d e" f')
        self.assertEqual(output, 'bar runs')

        self.assertEqual(fake_script_calls(self, clear=True), [['bar', 'd e', 'f']])

        self.assertEqual(fake_script_calls(self, clear=True), [])
