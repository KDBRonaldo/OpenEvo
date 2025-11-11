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

#!/usr/bin/env python3
import os
import re
import shutil
import tempfile
import time

from openhands.core.config import OpenHandsConfig, SandboxConfig
from openhands.events.action import BrowseInteractiveAction, CmdRunAction
from openhands.events.observation import BrowserOutputObservation
from openhands.events.stream import EventStream
from openhands.runtime.impl.singularity.singularity_runtime import SingularityRuntime
from openhands.storage import get_file_store

try:
    from PIL import Image
except Exception:
    Image = None


def demonstrate_browser_tool(
    runtime: SingularityRuntime, tool_name: str, action_command: str, description: str
):
    """Demonstrate a single browser tool with detailed input/output."""
    print(f'\n{"=" * 60}')
    print(f'🧪 TESTING: {tool_name}')
    print(f'📝 Description: {description}')
    print(f'{"=" * 60}')

    # Create action
    action = BrowseInteractiveAction(
        browser_actions=action_command, thought=f'Testing {tool_name}: {description}'
    )

    # Show input
    print('📥 INPUT:')
    print('   Action Type: BrowseInteractiveAction')
    print(f'   Browser Actions: {action.browser_actions}')
    print(f'   Thought: {action.thought}')

    # Execute action
    print('\n⚡ EXECUTING...')
    obs = runtime.run_action(action)

    # Show output
    print('\n📤 OUTPUT:')
    assert isinstance(obs, BrowserOutputObservation)

    print(f'   Success: {"✅ Yes" if not obs.error else "❌ No"}')
    print(f'   Current URL: {obs.url}')
    print(f'   Error: {obs.error}')
    print(f'   Screenshot Available: {"Yes" if obs.screenshot else "No"}')
    print(f'   Screenshot Path: {obs.screenshot_path or "None"}')
    print(f'   Focused Element ID: {obs.focused_element_bid or "None"}')
    print(
        f'   Page Content Length: {len(obs.content) if obs.content else 0} characters'
    )
    print(f'   Open Pages: {len(obs.open_pages_urls)}')
    print(f'   Last Action: {obs.last_browser_action}')

    if obs.set_of_marks:
        try:
            import base64
            from pathlib import Path

            # Determine host screenshots directory (avoid in-container paths like /workspace)
            host_ws = (
                getattr(runtime.config, 'workspace_base', None)
                or getattr(runtime.config, 'workspace_mount_path', None)
                or os.getcwd()
            )
            screenshot_dir = Path(host_ws) / '.browser_screenshots'
            screenshot_dir.mkdir(parents=True, exist_ok=True)

            # Reuse timestamp from screenshot filename if available, else generate one
            if obs.screenshot_path:
                timestamp = Path(obs.screenshot_path).stem.replace('screenshot_', '')
            else:
                timestamp = time.strftime('%Y%m%d_%H%M%S_%f')

            som_filename = f'som_{timestamp}.png'
            som_path = screenshot_dir / som_filename
            print(f'   ********SoM Image Path: {som_path}')

            # Decode and save SoM image
            som_data = obs.set_of_marks.replace('data:image/png;base64,', '')
            som_image_data = base64.b64decode(som_data)

            with open(som_path, 'wb') as f:
                f.write(som_image_data)

            print(f'   SoM Image Saved: {som_path}')
        except Exception as e:
            print(f'   SoM Save Error: {e}')

    if obs.last_browser_action_error:
        print(f'   Action Error: {obs.last_browser_action_error}')

    if hasattr(obs, 'get_agent_obs_text'):
        agent_text = obs.get_agent_obs_text()
        if agent_text:
            print('   Accessibility Tree Sample:')
            # print(f"   {agent_text[:500]}{'...' if len(agent_text) > 500 else ''}")
            print(f'   {agent_text}')

    return obs


def _cleanup_old_workspaces(prefix: str, keep_path: str | None) -> None:
    """
    Remove old temporary workspaces under /tmp matching the given prefix,
    except for 'keep_path' if provided.
    """
    try:
        tmp_dir = '/tmp'
        for name in os.listdir(tmp_dir):
            if not name.startswith(prefix):
                continue
            full = os.path.join(tmp_dir, name)
            if keep_path and os.path.abspath(full) == os.path.abspath(keep_path):
                continue
            if os.path.isdir(full):
                try:
                    shutil.rmtree(full, ignore_errors=True)
                except Exception:
                    pass
    except Exception:
        pass


def _save_screenshots_gif(
    workspace_dir: str,
    output_dir: str,
    gif_name: str = 'browser_run.gif',
    duration_ms: int = 700,
    image_prefix: str = 'screenshot_',
) -> str | None:
    try:
        screenshots_dir = os.path.join(workspace_dir, '.browser_screenshots')
        if not os.path.isdir(screenshots_dir):
            return None

        # Filter files by prefix and image extensions
        all_files = [
            f
            for f in os.listdir(screenshots_dir)
            if f.lower().endswith(('.png', '.jpg', '.jpeg'))
        ]
        files = [f for f in all_files if f.startswith(image_prefix)]

        if not files:
            return None
        files.sort()  # filenames contain timestamps; lexicographic order works

        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, gif_name)

        if Image is None:
            # PIL not available; skip silently
            return None

        frames = []
        for fname in files:
            p = os.path.join(screenshots_dir, fname)
            try:
                img = Image.open(p).convert('RGB')
                frames.append(img)
            except Exception:
                continue
        if not frames:
            return None

        frames[0].save(
            output_path,
            save_all=True,
            append_images=frames[1:],
            duration=duration_ms,
            loop=0,
            optimize=True,
            quality=85,
            format='GIF',
        )
        return output_path
    except Exception:
        return None


def main():
    """Main demonstration function."""
    print('🚀 Browser Tools Demonstration with Singularity Environment')
    print('=' * 60)

    # Setup workspace (clean up old runs first)
    # _cleanup_old_workspaces(prefix="browser_demo_", keep_path=None)
    workspace_dir = tempfile.mkdtemp(prefix='browser_demo_')
    # _cleanup_old_workspaces(prefix="browser_demo_", keep_path=workspace_dir)
    singularity_image_path = '/lustre/fsw/portfolios/llmservice/users/shaokunz/project/OpenHands_internal/singularity_images_oh/oh_v0.40.0_8me96m20iqt6tw9p_t5sffwjb6stny0ze.sif'

    print(f'📁 Workspace: {workspace_dir}')
    print(f'🖼️ Singularity Image: {singularity_image_path}')

    # Setup Singularity runtime
    sandbox_config = SandboxConfig(
        runtime_container_image=singularity_image_path,
        browsergym_eval_env=None,
        timeout=30,
        use_host_network=True,
    )

    config = OpenHandsConfig(
        default_agent='CodeActAgent',
        runtime='singularity',
        sandbox=sandbox_config,
        workspace_base=workspace_dir,
        workspace_mount_path=workspace_dir,
        run_as_openhands=False,  # This prevents the UID 0 conflict
    )

    # Setup event stream (required for SingularityRuntime)
    file_store = get_file_store('local', workspace_dir)
    event_stream = EventStream('browser_test_session', file_store)

    runtime = SingularityRuntime(
        config=config,
        event_stream=event_stream,
        sid='browser_test_session',
        plugins=[],
        headless_mode=True,
    )

    try:
        print('\n🔌 Connecting to runtime...')
        # Connect to the runtime (this starts the Singularity container and action server)
        import asyncio
        import os

        # Check if Singularity image exists
        if not os.path.exists(singularity_image_path):
            raise FileNotFoundError(
                f'Singularity image not found: {singularity_image_path}'
            )

        asyncio.run(runtime.connect())
        print('✅ Runtime connected successfully')

        # Google search demonstration - go to homepage first, then search for NVIDIA stock
        google_homepage = 'https://www.google.com'
        print(f'🌐 Starting at Google homepage: {google_homepage}')

        # 1. Navigation - goto Google homepage
        demonstrate_browser_tool(
            runtime,
            'goto(url)',
            f'goto("{google_homepage}")',
            'Navigate to Google homepage',
        )

        # 2. Wait for page to load and capture homepage
        obs_home = demonstrate_browser_tool(
            runtime,
            'noop(wait_ms)',
            'noop(2000)',
            'Wait for Google homepage to fully load',
        )

        nvidia_url = 'https://www.nvidia.com/en-us/'
        demonstrate_browser_tool(
            runtime, 'goto(url)', f'goto("{nvidia_url}")', 'Navigate to NVIDIA homepage'
        )
        demonstrate_browser_tool(
            runtime, 'noop(wait_ms)', 'noop(1500)', 'Wait for GitHub to load'
        )
        for _ in range(8):
            demonstrate_browser_tool(
                runtime,
                'scroll(delta_x, delta_y)',
                'scroll(0, 1200)',
                'Scroll down GitHub page',
            )
            demonstrate_browser_tool(
                runtime,
                'noop(wait_ms)',
                'noop(200)',
                'Small wait between GitHub scrolls',
            )

        demonstrate_browser_tool(
            runtime,
            'goto(url)',
            f'goto("{google_homepage}")',
            'Navigate to Google homepage',
        )

        # 2. Wait for page to load and capture homepage
        obs_home = demonstrate_browser_tool(
            runtime,
            'noop(wait_ms)',
            'noop(2000)',
            'Wait for Google homepage to fully load',
        )

        # 3. Find and type in the search box
        # Note: We'll need to identify the search box element dynamically
        print("\n🔍 Now attempting to search for 'nvidia stock'...")
        print('📋 This will search for the Google search input field and type in it')

        # Use the most robust approach - click the search box and then type
        # From the accessibility tree, we can see [104] is the search combobox
        # Parse accessibility tree to close modal if present and detect search box BID
        ax_text = (
            obs_home.get_agent_obs_text()
            if hasattr(obs_home, 'get_agent_obs_text')
            else ''
        )

        # 3.a Close modal dialog if present (e.g., 'Waiting...' overlay)
        close_btn_bid = None
        if ax_text:
            m_close = re.search(
                r'^\s*\[(\d+)\]\s+button \'close\'', ax_text, flags=re.MULTILINE
            )
            if m_close:
                close_btn_bid = m_close.group(1)
        if close_btn_bid:
            demonstrate_browser_tool(
                runtime, 'click(bid)', f'click("{close_btn_bid}")', 'Close modal dialog'
            )
            # Refresh tree after closing
            obs_home = demonstrate_browser_tool(
                runtime, 'noop(wait_ms)', 'noop(500)', 'Wait after closing modal'
            )
            ax_text = (
                obs_home.get_agent_obs_text()
                if hasattr(obs_home, 'get_agent_obs_text')
                else ax_text
            )

        # 3.b Find the Google search combobox BID dynamically
        search_bid = None
        if ax_text:
            m_search = re.search(
                r'^\s*\[(\d+)\]\s+combobox \'Search\'', ax_text, flags=re.MULTILINE
            )
            if m_search:
                search_bid = m_search.group(1)

        if not search_bid:
            print(
                '⚠️ Could not detect search combobox BID from accessibility tree. Aborting search steps.'
            )
        else:
            steps = [
                (f'click("{search_bid}")', 'Focus the search box'),
                (f'fill("{search_bid}", "nvidia stock")', 'Type query into search box'),
                (f'press("{search_bid}", "Enter")', 'Submit the search'),
            ]
            for i, (action, description) in enumerate(steps, 1):
                demonstrate_browser_tool(
                    runtime, f'Search Step {i}', action, description
                )

        # 7. Wait for search results to load
        demonstrate_browser_tool(
            runtime, 'noop(wait_ms)', 'noop(3000)', 'Wait for search results to load'
        )

        # 8. Scroll down to see more results
        demonstrate_browser_tool(
            runtime,
            'scroll(delta_x, delta_y)',
            'scroll(0, 500)',
            'Scroll down to see more search results',
        )

        # 9. Go back to previous page (expected: Google search/home)
        demonstrate_browser_tool(
            runtime, 'go_back()', 'go_back()', 'Go back to the previous page'
        )
        obs_after_back = demonstrate_browser_tool(
            runtime, 'noop(wait_ms)', 'noop(1500)', 'Wait after going back'
        )

        # 10. Click 'About' link dynamically
        about_ax = (
            obs_after_back.get_agent_obs_text()
            if hasattr(obs_after_back, 'get_agent_obs_text')
            else ''
        )
        about_bid = None
        if about_ax:
            m_about = re.search(
                r'^\s*\[(\d+)\]\s+link \'About\'', about_ax, flags=re.MULTILINE
            )
            if m_about:
                about_bid = m_about.group(1)
        if about_bid:
            demonstrate_browser_tool(
                runtime, 'click(bid)', f'click("{about_bid}")', "Click 'About' link"
            )
            demonstrate_browser_tool(
                runtime, 'noop(wait_ms)', 'noop(1500)', 'Wait for About page to load'
            )
        else:
            print("⚠️ 'About' link not found on this page; skipping click.")

        # 11. Scroll down to the end (multiple scrolls)
        for _ in range(6):
            demonstrate_browser_tool(
                runtime, 'scroll(delta_x, delta_y)', 'scroll(0, 1000)', 'Scroll down'
            )
            demonstrate_browser_tool(
                runtime, 'noop(wait_ms)', 'noop(300)', 'Small wait between scrolls'
            )

        # 12. Come back to the home page
        demonstrate_browser_tool(
            runtime, 'go_back()', 'go_back()', 'Return to the previous page (home)'
        )
        demonstrate_browser_tool(
            runtime, 'noop(wait_ms)', 'noop(1500)', 'Wait after returning to home'
        )

        print('\n🎉 Browser Tools Demonstration Completed!')
        print('📊 Demonstrated 13 different browser tools with detailed input/output')
        print(f'📁 Test files available in: {workspace_dir}')

        # Save run GIFs into logs directory
        logs_dir = '/lustre/fsw/portfolios/llmservice/users/shaokunz/project/OpenHands_internal/logs'

        # Create GIF from regular screenshots
        gif_path = _save_screenshots_gif(
            workspace_dir,
            logs_dir,
            gif_name='browser_run.gif',
            duration_ms=700,
            image_prefix='screenshot_',
        )
        if gif_path:
            print(f'🎞️ Saved Regular Screenshots GIF: {gif_path}')
        else:
            print(
                '⚠️ No regular screenshots GIF saved (no screenshots or PIL not available)'
            )

        # Create GIF from SoM augmented screenshots
        som_gif_path = _save_screenshots_gif(
            workspace_dir,
            logs_dir,
            gif_name='browser_run_som.gif',
            duration_ms=700,
            image_prefix='som_',
        )
        if som_gif_path:
            print(f'🎞️ Saved SoM Screenshots GIF: {som_gif_path}')
        else:
            print('⚠️ No SoM screenshots GIF saved (no SoM images or PIL not available)')

    except Exception as e:
        print(f'\n❌ Demonstration failed: {str(e)}')
        import traceback

        traceback.print_exc()

    finally:
        # Cleanup
        cmd = CmdRunAction(command="pkill -f 'python3 -m http.server'")
        runtime.run_action(cmd)
        print('\n🧹 Cleanup completed')


if __name__ == '__main__':
    main()
