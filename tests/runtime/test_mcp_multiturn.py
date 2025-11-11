# Copyright 2025 NVIDIA CORPORATION & AFFILIATES
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""
Test for MCP resource leak detection.

This test reproduces the scenario where a single runtime instance
repeatedly calls MCP tools, potentially causing resource exhaustion.
"""

import asyncio
import json
import socket
import subprocess
import time
import uuid

import pytest

from openhands.core.config import load_openhands_config
from openhands.core.config.mcp_config import (
    MCPSSEServerConfig,
    MCPStdioServerConfig,
)
from openhands.events import EventStream
from openhands.events.action.mcp import MCPAction
from openhands.runtime.impl.singularity.singularity_runtime import SingularityRuntime
from openhands.runtime.plugins import AgentSkillsRequirement, JupyterRequirement
from openhands.storage import get_file_store

MAX_NUM_CALLS = 40


def _start_sse_mcp_singularity_server() -> tuple[subprocess.Popen[str], str]:
    """Start the SSE MCP server using Singularity and return (process, sse_url)."""
    # Ensure Singularity is available
    try:
        subprocess.run(
            ['singularity', '--version'], check=True, capture_output=True, text=True
        )
    except Exception as e:
        pytest.skip(f'Singularity is not available on this system: {e}')

    image_ref = 'docker://supercorp/supergateway'

    # Find a free port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        host_port = s.getsockname()[1]

    # Bind the server directly to the chosen host port
    container_port = host_port

    # Command line mirrors the Docker args passed to the image entrypoint
    # Use the same format as in test_mcp_parallel.py
    run_cmd = [
        'singularity',
        'run',
        image_ref,
        '--stdio',
        'npx -y @modelcontextprotocol/server-filesystem /tmp',
        '--port',
        str(container_port),
        '--baseUrl',
        f'http://localhost:{host_port}',
    ]

    proc = subprocess.Popen(
        run_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

    # Give the server time to initialize
    time.sleep(10)  # Increase wait time from 3 seconds to 10 seconds

    return proc, f'http://localhost:{host_port}/sse'


def _stop_process(proc: subprocess.Popen[str]) -> None:
    """Helper to properly stop a process."""
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception:
        pass


@pytest.mark.asyncio
async def test_mcp_resource_leak_single_runtime(temp_dir):
    """
    Test that reproduces resource leak when a single runtime
    repeatedly calls MCP fetch tool on public websites.
    """
    # Start MCP server
    proc, sse_url = _start_sse_mcp_singularity_server()

    # Use a stable list of public websites
    test_urls = [
        'https://www.example.com',
        'https://httpbin.org/html',
        'https://httpbin.org/json',
        'https://httpbin.org/status/200',
        'https://www.google.com',
        'https://api.github.com',
        'https://jsonplaceholder.typicode.com/posts/1',
        'https://www.wikipedia.org',
        'https://www.cloudflare.com',
        'https://httpbin.org/headers',
        'https://httpbin.org/user-agent',
        'https://httpbin.org/ip',
        'https://httpbin.org/uuid',
        'https://httpbin.org/base64/SFRUUEJJTiBpcyBhd2Vzb21l',
        'https://httpbin.org/delay/1',
    ]

    try:
        # Setup runtime configuration
        config = load_openhands_config()
        config.run_as_openhands = False
        config.runtime = 'singularity'
        config.workspace_base = temp_dir
        config.workspace_mount_path = temp_dir

        # Configure MCP
        config.mcp.sse_servers.clear()
        config.mcp.sse_servers.append(MCPSSEServerConfig(url=sse_url))

        # Add fetch stdio server
        config.mcp.stdio_servers.clear()
        config.mcp.stdio_servers.append(
            MCPStdioServerConfig(name='fetch', command='uvx', args=['mcp-server-fetch'])
        )

        # Create event stream
        sid = f'test_leak_{uuid.uuid4().hex[:8]}'
        event_stream = EventStream(
            sid,
            file_store=get_file_store(config.file_store, config.file_store_path),
        )

        # Create single runtime instance
        runtime = SingularityRuntime(
            config=config,
            event_stream=event_stream,
            sid=sid,
            plugins=[JupyterRequirement(), AgentSkillsRequirement()],
        )

        await runtime.connect()

        # Track successful calls
        successful_calls = 0
        failed_calls = 0
        timeout_errors = 0

        # Repeatedly call MCP fetch tool
        for i in range(MAX_NUM_CALLS):  # Try MAX_NUM_CALLS calls
            print(f'\n=== MCP Call {i + 1}/{MAX_NUM_CALLS} ===')
            url = test_urls[i % len(test_urls)]  # Cycle through URLs
            print(f'Fetching: {url}')

            action = MCPAction(name='fetch', arguments={'url': url})

            try:
                start_time = time.time()
                obs = await asyncio.wait_for(
                    runtime.call_tool_mcp(action),
                    timeout=35,  # 35 second timeout
                )
                elapsed = time.time() - start_time

                # Parse MCPObservation
                if obs:
                    data = json.loads(obs.content)
                    is_error = data.get('isError', False)

                    if not is_error:
                        successful_calls += 1
                        print(f'✓ Call {i + 1} succeeded in {elapsed:.2f}s')
                        # 验证获取到了内容
                        text = ''
                        for c in data.get('content', []):
                            if c.get('type') == 'text' and isinstance(
                                c.get('text'), str
                            ):
                                text = c['text']
                                break
                        # Only check if there is content, do not check the specific content
                        assert len(text) > 0, f'No content fetched from {url}'
                        print(f'  Fetched {len(text)} characters')
                    else:
                        failed_calls += 1
                        error_msg = data.get('text', 'Unknown error')
                        print(f'✗ Call {i + 1} failed: {error_msg}')
                else:
                    failed_calls += 1
                    print(f'✗ Call {i + 1} failed: No observation returned')

            except asyncio.TimeoutError:
                timeout_errors += 1
                print(f'✗ Call {i + 1} TIMEOUT after 35s')
                # This is the resource leak indicator!

            except Exception as e:
                failed_calls += 1
                print(f'✗ Call {i + 1} exception: {e}')

        # Report results
        print('\n=== Results ===')
        print(f'Successful calls: {successful_calls}/{MAX_NUM_CALLS}')
        print(f'Failed calls: {failed_calls}/{MAX_NUM_CALLS}')
        print(f'Timeout errors: {timeout_errors}/{MAX_NUM_CALLS}')

        # The test fails if we see the resource leak pattern
        if timeout_errors > 0 and successful_calls > 5:
            pytest.fail(
                f'Resource leak detected! '
                f'Initial calls succeeded ({successful_calls}), '
                f'but then {timeout_errors} timeouts occurred. '
                f'This indicates connection pool exhaustion.'
            )

        # Also fail if too many calls failed
        assert successful_calls >= 13, (
            f'Too many failed calls: only {successful_calls}/{MAX_NUM_CALLS} succeeded'
        )

        # Clean up runtime
        runtime.close()

    finally:
        # Clean up MCP server
        _stop_process(proc)


@pytest.mark.asyncio
async def test_mcp_resource_leak_with_reconnect(temp_dir):
    """
    Test that shows reconnecting between calls avoids resource leak
    using fetch on public websites.
    """
    # Start MCP server
    proc, sse_url = _start_sse_mcp_singularity_server()

    # Use a stable test URL
    test_url = 'https://www.example.com'

    try:
        successful_calls = 0

        # Create fresh runtime for each call
        for i in range(MAX_NUM_CALLS):
            print(f'\n=== Fresh Runtime {i + 1}/{MAX_NUM_CALLS} ===')

            # Setup configuration for each runtime
            config = load_openhands_config()
            config.run_as_openhands = False
            config.runtime = 'singularity'
            config.workspace_base = temp_dir
            config.workspace_mount_path = temp_dir

            # Configure MCP with fetch
            config.mcp.sse_servers.clear()
            config.mcp.sse_servers.append(MCPSSEServerConfig(url=sse_url))

            config.mcp.stdio_servers.clear()
            config.mcp.stdio_servers.append(
                MCPStdioServerConfig(
                    name='fetch', command='uvx', args=['mcp-server-fetch']
                )
            )

            # Create event stream
            sid = f'test_reconnect_{i}_{uuid.uuid4().hex[:8]}'
            event_stream = EventStream(
                sid,
                file_store=get_file_store(config.file_store, config.file_store_path),
            )

            # Create new runtime instance
            runtime = SingularityRuntime(
                config=config,
                event_stream=event_stream,
                sid=sid,
                plugins=[JupyterRequirement(), AgentSkillsRequirement()],
            )

            await runtime.connect()

            # Single MCP fetch call
            action = MCPAction(name='fetch', arguments={'url': test_url})

            try:
                obs = await asyncio.wait_for(runtime.call_tool_mcp(action), timeout=35)

                # Parse MCPObservation
                if obs:
                    data = json.loads(obs.content)
                    is_error = data.get('isError', False)

                    if not is_error:
                        successful_calls += 1
                        print(f'✓ Runtime {i + 1} succeeded')
                        # Verify that content is present
                        text = ''
                        for c in data.get('content', []):
                            if c.get('type') == 'text' and isinstance(
                                c.get('text'), str
                            ):
                                text = c['text']
                                break
                        assert len(text) > 0, 'No content fetched'
                        print(f'  Fetched {len(text)} characters from {test_url}')
                    else:
                        print(f'✗ Runtime {i + 1} failed with error')
                else:
                    print(f'✗ Runtime {i + 1} failed: No observation returned')

            except asyncio.TimeoutError:
                print(f'✗ Runtime {i + 1} TIMEOUT')

            finally:
                # Immediately close this runtime
                runtime.close()

        print('\n=== Results ===')
        print(f'Successful calls: {successful_calls}/{MAX_NUM_CALLS}')

        # This should work fine because each runtime is fresh
        assert successful_calls == MAX_NUM_CALLS, (
            f'Fresh runtime approach should work: only {successful_calls}/{MAX_NUM_CALLS} succeeded'
        )

    finally:
        # Clean up MCP server
        _stop_process(proc)


if __name__ == '__main__':
    # Run the tests
    pytest.main([__file__, '-v', '-s'])
