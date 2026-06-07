#!/usr/bin/env python3
"""Unit tests for workspace-controller/controller.py"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

# Add the parent directory to the path so we can import controller
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock the subprocess.run before importing controller
# since controller uses it at module level
with mock.patch('subprocess.run'):
    import controller


class TestControllerNamespaceDetection(unittest.TestCase):
    """Test namespace detection functionality."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.namespace_file = os.path.join(self.temp_dir, 'namespace')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_detect_namespace_from_file(self):
        """Test reading namespace from Kubernetes service account file."""
        with open(self.namespace_file, 'w') as f:
            f.write('test-namespace\n')
        
        with mock.patch('controller.NAMESPACE_FILE', self.namespace_file):
            namespace = controller.detect_namespace()
            self.assertEqual(namespace, 'test-namespace')

    def test_detect_namespace_fallback_to_env(self):
        """Test falling back to NAMESPACE environment variable."""
        # Create a non-readable file
        import stat
        os.chmod(self.namespace_file, 0o000)
        
        with mock.patch('controller.NAMESPACE_FILE', self.namespace_file):
            with mock.patch.dict('os.environ', {'NAMESPACE': 'env-namespace'}):
                namespace = controller.detect_namespace()
                self.assertEqual(namespace, 'env-namespace')

    def test_detect_namespace_default_fallback(self):
        """Test default fallback to 'coder' namespace."""
        # Remove the file entirely
        if os.path.exists(self.namespace_file):
            os.unlink(self.namespace_file)
        
        with mock.patch('controller.NAMESPACE_FILE', self.namespace_file):
            with mock.patch.dict('os.environ', {}, clear=True):
                namespace = controller.detect_namespace()
                self.assertEqual(namespace, 'coder')


class TestControllerKubectlOperations(unittest.TestCase):
    """Test kubectl operation methods."""

    def setUp(self):
        self.controller = controller
        # Mock subprocess.run globally for these tests
        self.subprocess_patcher = mock.patch('subprocess.run')
        self.mock_run = self.subprocess_patcher.start()

    def tearDown(self):
        self.subprocess_patcher.stop()

    def test_get_deployments_success(self):
        """Test successful retrieval of deployments."""
        mock_output = json.dumps({
            'items': [
                {
                    'metadata': {'name': 'ws-user1', 'namespace': 'coder'},
                    'status': {'readyReplicas': 1, 'replicas': 1}
                },
                {
                    'metadata': {'name': 'ws-user2', 'namespace': 'coder'},
                    'status': {'readyReplicas': 0, 'replicas': 0}
                },
                {
                    'metadata': {'name': 'other-deployment', 'namespace': 'coder'},
                    'status': {'readyReplicas': 1, 'replicas': 1}
                }
            ]
        })
        
        mock_result = mock.Mock()
        mock_result.returncode = 0
        mock_result.stdout = mock_output
        self.mock_run.return_value = mock_result
        
        deployments = self.controller._get_deployments('coder')
        
        # Should only return deployments with ws- prefix
        self.assertEqual(len(deployments), 2)
        self.assertEqual(deployments[0]['name'], 'ws-user1')
        self.assertEqual(deployments[1]['name'], 'ws-user2')

    def test_get_deployments_kubectl_failure(self):
        """Test handling of kubectl failure."""
        mock_result = mock.Mock()
        mock_result.returncode = 1
        mock_result.stderr = 'kubectl error'
        self.mock_run.return_value = mock_result
        
        deployments = self.controller._get_deployments('coder')
        self.assertEqual(deployments, [])

    def test_scale_deployment_success(self):
        """Test successful scaling of a deployment."""
        mock_result = mock.Mock()
        mock_result.returncode = 0
        mock_result.stdout = 'deployment.apps/ws-user scaled'
        self.mock_run.return_value = mock_result
        
        success = self.controller._scale_deployment('coder', 'ws-user', 1)
        self.assertTrue(success)
        
        # Verify kubectl was called with correct arguments
        self.mock_run.assert_called_once()
        call_args = self.mock_run.call_args[0][0]
        self.assertIn('kubectl', call_args)
        self.assertIn('scale', call_args)
        self.assertIn('--namespace=coder', call_args)
        self.assertIn('deployment/ws-user', call_args)
        self.assertIn('--replicas=1', call_args)

    def test_scale_deployment_failure(self):
        """Test handling of scaling failure."""
        mock_result = mock.Mock()
        mock_result.returncode = 1
        mock_result.stderr = 'deployment not found'
        self.mock_run.return_value = mock_result
        
        success = self.controller._scale_deployment('coder', 'ws-user', 1)
        self.assertFalse(success)


class TestControllerHTTPHandler(unittest.TestCase):
    """Test HTTP request handler functionality."""

    def setUp(self):
        self.controller = controller
        
        # Create a mock request handler
        self.handler = mock.Mock()
        self.handler.headers = {}
        self.handler.path = '/api/workspaces'
        self.handler.client_address = ('127.0.0.1', 12345)
        
        # Mock handler methods
        self.handler.send_response = mock.Mock()
        self.handler.send_header = mock.Mock()
        self.handler.end_headers = mock.Mock()
        self.handler.wfile = mock.Mock()

    def test_extract_user_from_deployment_name(self):
        """Test extraction of username from deployment name."""
        test_cases = [
            ('ws-john', 'john'),
            ('ws-john-doe', 'john-doe'),
            ('ws-john_doe', 'john_doe'),
            ('ws-john123', 'john123'),
            ('not-ws-prefix', None),
            ('ws-', None),  # Empty username
        ]
        
        for deployment_name, expected_username in test_cases:
            with self.subTest(deployment_name=deployment_name):
                username = self.controller._extract_username_from_deployment(deployment_name)
                self.assertEqual(username, expected_username)

    def test_parse_query_params(self):
        """Test parsing of query parameters."""
        handler = mock.Mock()
        handler.path = '/api/workspaces?status=running&limit=10'
        
        params = self.controller._parse_query_params(handler)
        self.assertEqual(params.get('status'), 'running')
        self.assertEqual(params.get('limit'), '10')
        
        # Test URL decoding
        handler.path = '/api/workspaces?search=test%20query&encoded=true'
        params = self.controller._parse_query_params(handler)
        self.assertEqual(params.get('search'), 'test query')
        self.assertEqual(params.get('encoded'), 'true')

    def test_send_json_response(self):
        """Test sending JSON response."""
        data = {'status': 'ok', 'workspaces': []}
        
        # Patch the handler methods
        with mock.patch.object(self.handler, 'send_response') as mock_send_response, \
             mock.patch.object(self.handler, 'send_header') as mock_send_header, \
             mock.patch.object(self.handler, 'end_headers') as mock_end_headers:
            
            mock_wfile = mock.Mock()
            self.handler.wfile = mock_wfile
            
            self.controller._send_json_response(self.handler, data, 200)
            
            # Verify response was sent correctly
            mock_send_response.assert_called_once_with(200)
            mock_send_header.assert_any_call('Content-type', 'application/json')
            mock_send_header.assert_any_call('Cache-Control', 'no-cache, must-revalidate')
            mock_end_headers.assert_called_once()
            
            # Verify JSON was written
            written_data = mock_wfile.write.call_args[0][0]
            parsed_data = json.loads(written_data.decode('utf-8'))
            self.assertEqual(parsed_data, data)

    def test_send_error_response(self):
        """Test sending error response."""
        with mock.patch.object(self.controller, '_send_json_response') as mock_send_json:
            self.controller._send_error_response(
                self.handler, 
                404, 
                'Workspace not found',
                {'workspace': 'nonexistent'}
            )
            
            mock_send_json.assert_called_once()
            call_args = mock_send_json.call_args[0]
            self.assertEqual(call_args[0], self.handler)
            self.assertEqual(call_args[2], 404)
            
            error_data = call_args[1]
            self.assertEqual(error_data['error'], 'Workspace not found')
            self.assertEqual(error_data['code'], 404)
            self.assertEqual(error_data['details'], {'workspace': 'nonexistent'})


class TestControllerAuthentication(unittest.TestCase):
    """Test authentication and authorization."""

    def setUp(self):
        self.controller = controller
        
    def test_authenticate_request_valid_auth(self):
        """Test authentication with valid proxy headers."""
        handler = mock.Mock()
        handler.headers = {
            'X-Auth-Request-User': 'admin@example.com',
            'X-Auth-Request-Email': 'admin@example.com'
        }
        
        with mock.patch.dict('os.environ', {
            'TRUSTED_PROXY': 'true',
            'ADMIN_USERS': 'admin@example.com,other@example.com'
        }):
            user = self.controller._authenticate_request(handler)
            self.assertEqual(user, 'admin@example.com')

    def test_authenticate_request_no_proxy_trust(self):
        """Test authentication when proxy is not trusted."""
        handler = mock.Mock()
        handler.headers = {'X-Auth-Request-User': 'user@example.com'}
        
        with mock.patch.dict('os.environ', {'TRUSTED_PROXY': 'false'}):
            user = self.controller._authenticate_request(handler)
            self.assertIsNone(user)

    def test_authenticate_request_user_not_in_allowlist(self):
        """Test authentication when user is not in admin allowlist."""
        handler = mock.Mock()
        handler.headers = {'X-Auth-Request-User': 'unauthorized@example.com'}
        
        with mock.patch.dict('os.environ', {
            'TRUSTED_PROXY': 'true',
            'ADMIN_USERS': 'admin@example.com'
        }):
            user = self.controller._authenticate_request(handler)
            self.assertIsNone(user)

    def test_authenticate_request_no_admin_users_config(self):
        """Test authentication when ADMIN_USERS is not set (allow all)."""
        handler = mock.Mock()
        handler.headers = {'X-Auth-Request-User': 'anyuser@example.com'}
        
        with mock.patch.dict('os.environ', {
            'TRUSTED_PROXY': 'true',
            'ADMIN_USERS': ''
        }):
            user = self.controller._authenticate_request(handler)
            self.assertEqual(user, 'anyuser@example.com')


class TestControllerWorkspaceOperations(unittest.TestCase):
    """Test workspace listing and operations."""

    def setUp(self):
        self.controller = controller
        self.subprocess_patcher = mock.patch('subprocess.run')
        self.mock_run = self.subprocess_patcher.start()
        
        # Setup mock kubectl output
        mock_output = json.dumps({
            'items': [
                {
                    'metadata': {
                        'name': 'ws-user1',
                        'namespace': 'coder',
                        'creationTimestamp': '2024-01-01T00:00:00Z',
                        'labels': {'app': 'workspace'}
                    },
                    'status': {
                        'readyReplicas': 1,
                        'replicas': 1,
                        'conditions': [
                            {'type': 'Available', 'status': 'True'}
                        ]
                    },
                    'spec': {
                        'replicas': 1,
                        'template': {
                            'spec': {
                                'containers': [
                                    {
                                        'name': 'ide',
                                        'resources': {
                                            'requests': {'cpu': '500m', 'memory': '2Gi'},
                                            'limits': {'cpu': '2', 'memory': '6Gi'}
                                        }
                                    }
                                ]
                            }
                        }
                    }
                },
                {
                    'metadata': {
                        'name': 'ws-user2',
                        'namespace': 'coder'
                    },
                    'status': {
                        'readyReplicas': 0,
                        'replicas': 0
                    },
                    'spec': {
                        'replicas': 0
                    }
                }
            ]
        })
        
        mock_result = mock.Mock()
        mock_result.returncode = 0
        mock_result.stdout = mock_output
        self.mock_run.return_value = mock_result

    def tearDown(self):
        self.subprocess_patcher.stop()

    def test_list_workspaces(self):
        """Test listing workspaces."""
        workspaces = self.controller._list_workspaces('coder')
        
        self.assertEqual(len(workspaces), 2)
        
        # Check first workspace
        ws1 = workspaces[0]
        self.assertEqual(ws1['name'], 'ws-user1')
        self.assertEqual(ws1['user'], 'user1')
        self.assertEqual(ws1['status'], 'running')
        self.assertEqual(ws1['ready'], 1)
        self.assertEqual(ws1['total'], 1)
        self.assertTrue(ws1['available'])
        
        # Check second workspace
        ws2 = workspaces[1]
        self.assertEqual(ws2['name'], 'ws-user2')
        self.assertEqual(ws2['user'], 'user2')
        self.assertEqual(ws2['status'], 'stopped')
        self.assertEqual(ws2['ready'], 0)
        self.assertEqual(ws2['total'], 0)

    def test_get_workspace_metrics(self):
        """Test getting workspace metrics."""
        # This would test Prometheus querying
        # For now, just ensure the method exists and handles errors
        with mock.patch('urllib.request.urlopen') as mock_urlopen:
            mock_response = mock.Mock()
            mock_response.read.return_value = json.dumps({
                'data': {'result': []}
            }).encode('utf-8')
            mock_urlopen.return_value = mock_response
            
            metrics = self.controller._get_workspace_metrics('ws-user1', 'coder')
            self.assertEqual(metrics, [])


if __name__ == '__main__':
    unittest.main()