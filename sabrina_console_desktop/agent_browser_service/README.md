# Browser missions

## Existing Ego browser fleet

Choose **Ego browser fleet (temporary)**, enter an HTTPS starting page, and launch a mission. Sabrina connects to the existing `browser_fleet_node_1` on the configured Docker host and creates a new isolated Playwright context for that mission. The worker uses the Playwright already installed in the fleet node, so the desktop's Playwright version does not need to match it. No files are installed in the running fleet container, and the shared node is left running after the mission.

The context is temporary and is discarded when the mission ends. It does not use your Windows Edge profile; a guest cart stored only in this context will disappear. Use **Your Microsoft Edge** when the result must remain in your own browser. Fleet missions limit navigation to the selected HTTPS origin and reject non-public DNS answers; public third-party page resources may load. The fleet is a shared browser service, not the network-isolated container backend below. Sites requiring a user verification need a visible browser.

The fleet defaults to the `ego` SSH alias. Set `SABRINA_FLEET_DOCKER_SSH` to `local` or another configured SSH destination to select a different fleet host; if unset, the shared `SABRINA_BROWSER_DOCKER_SSH` setting is also honored. `SABRINA_FLEET_NODE` can select `browser_fleet_node_1` through `browser_fleet_node_4`.

## A selected desktop window

Choose **Vercel desktop**, refresh the window list, and select the application window for the mission. This uses Vercel's `agent-desktop` locally; it does not need browser remote debugging or a Docker container. Sabrina passes the selected window and its current accessibility snapshot to the coordinator and execution specialist. For supported browsers, snapshots are restricted to the active page and address bar. The selected window stays open after the mission.

The default uses accessibility actions. Select **Use mouse and keyboard** to permit foreground and physical input; this may move the cursor or bring the window forward. When an accessibility click cannot distinguish identical controls, the agent may explicitly use the visible control's position after the runtime checks the selected window and point. Choose **Let me act first** to pause before the agent starts. If a page requests sign-in or verification, handle it yourself and choose **Continue mission**.

The Windows x64 runtime is included in `agent_desktop_service/bin` with its Apache 2.0 license. To use another trusted runtime, set `SABRINA_AGENT_DESKTOP_PATH` to its executable path. A mission uses temporary, private agent-desktop session state; it does not close the selected application at the end. Page content from that selected window is sent to the model chosen for the mission, so use a local model when that content must stay local.

## Your browser

Choose **Your Microsoft Edge** or **Your Google Chrome** in the quick launcher. Open **Set up browser** and enable **Allow remote debugging for this browser instance**. Enter the mission and its HTTPS starting page, then launch. The browser asks you to allow the connection. Sabrina opens one new tab in your existing profile.

When the site needs a security check or sign-in, Sabrina pauses. Complete it in your browser and choose **Continue mission** in the console. Choose **Let me act first** when you want Sabrina to pause immediately after opening the page. The tab stays open after the mission so its contents and cart remain in your browser session. Browser resources such as security verification scripts may load from the domains the site uses.

The desktop Python environment needs `playwright==1.65.2`. Install the package from `requirements-browser.txt` when setting up another desktop environment.

## Container browser

Choose **Vercel agent-browser** or **Microsoft Playwright CLI** in the Swarm Monitor quick launcher. Enter the mission and the HTTPS starting page, then launch. Sabrina sends the coordinator the selected tool and site origin. The execution specialist receives a fresh accessibility snapshot after each browser action. The mission report records the last observations, browser time, and model tokens.

## Docker setup

From the Sabrina project directory, build the image once with the same Python that launches the desktop.

```powershell
python -m sabrina_console_desktop.agent_browser_service
```

The desktop uses local Docker when installed. On Windows without Docker, it uses the existing `ego` OpenSSH alias. Set `SABRINA_BROWSER_DOCKER_SSH` to `local` or a configured SSH destination to choose the engine explicitly. SSH uses the caller's existing credentials and verified host key. The image is based on Microsoft's Playwright image and includes both pinned CLIs.

Each mission starts its container on the first browser action and discards it after browser work finishes. It runs as the image's unprivileged user with no host volumes, published ports, or existing browser profile. Browser requests use one exact HTTPS origin. Sites that depend on cross-origin redirects or assets may need a future multi-origin configuration. State confined to the guest profile, including some carts, disappears at cleanup. A site-provided permanent link can be recorded in the mission report.
