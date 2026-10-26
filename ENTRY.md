# User Entry Point

- [Flashing](#flashing)
- [IP and SSH Configuration](#ip-and-ssh-configuration)
- [File Paths](#file-paths)
- [Terminal Commands](#terminal-commands)
- [Operating Modes](#operating-modes)
- [AI Pauses](#ai-pauses)
- [Pwngrid Integration](#pwngrid-integration)
- [Plugins](#plugins)
- [System Resilience & SD Protection](#system-resilience--sd-protection)
- [Troubleshooting & Diagnostics](#troubleshooting--diagnostics)
- [System Architecture & Hardware Updates](#system-architecture--hardware-updates)

## Flashing
The easiest way is to use [**Raspberry Pi Imager**](https://raspberrypi.com); everything there is extremely straightforward and doesn't require instructions.

## IP and SSH Configuration
To connect to your Pwnagotchi, plug the device into your computer's data port and configure your OS network adapter settings:
* **Share your Internet connection** from your main internet adapter to the Pwnagotchi adapter (Remote NDIS/USB Ethernet).
* Assign a static IP to the Pwnagotchi network interface:
  * **IP address:** `10.0.0.1`
  * **Subnet mask:** `255.255.255.0`

Once configured, you can access the device using the following credentials:
* **SSH Connection:** `ssh pi@10.0.0.2` (Default password: `raspberry`)
* **Web UI:** Navigate to `http://10.0.0.2:8080` in your browser.

## File Paths
You can manage your device settings and files using the following paths:
* `/etc/pwnagotchi/config.toml` - User configuration file for editing.  
*Note: You can easily generate or update this file interactively using the `pwnygen` terminal command.*
* `/etc/pwnagotchi/default.toml` - Automatically regenerated config used for viewing default parameters.
* `/home/pi/handshakes/` - Default handshakes folder location. It is configured with full read/write permissions for all users, making file transfers via SSH or SFTP clients (like FileZilla) seamless.
* `/home/pi/custom_plugins/` - Directory for your custom plugins.

## Terminal Commands
You can interactively manage the core features, battery power modules, and states of your device directly from the CLI using the following built-in commands:
* `pwnygen` - The most essential; launches an interactive generator to create or modify your config.toml configuration file. This utility is also required for configuring PiSugar power management packages, triggering interactive **debconf** terminal windows to easily select your specific hardware revision.
* `pwnystatus` - Displays the current operational status, active modes, and health metrics of the unit.
* `pwnyauto` - Forces the device back into automatic scanning and network interaction mode.
* `pwnykill` - Safely stops the main Pwnagotchi processes, AI scripts, and active routines.
* `pwnylog` - Launches a real-time terminal output stream for monitoring system and service logs.

## Operating Modes
The Pwnagotchi operates in several modes depending on its state and the AI status:

### Standard Modes
* **AUTO**: The default automated mode where the device independently scans for networks and handshakes.
* **MANU**: Manual mode, activated when connecting via computer, allowing manual control and stopping automatic scanning.

### New Modes
* **WAIT**: Displayed during the initial bootstrapping process on Epoch 0, such as setting up PyTorch dependencies or generating Pwnagotchi RSA keys.
* **EVAL**: Neural network inference mode. In this state, the AI is actively running its pre-trained model to make real-time decisions based on past experience, optimizing its behavior without modifying the underlying weights.
* **LEARN**: Active training mode. The AI is processing new data in real time, progressing through pure training epochs to adjust its neural network weights and adapt to the environment.

## AI Pauses
The system logic handles automatic pauses during operation based on specific conditions:
* **Home Networks & Bored/Sad Triggers**: The unit automatically pauses training when any Wi-Fi networks specified in your `home_networks` list are detected, or when it enters a `bored` or `sad` state.
* **Why it Pauses**: Stationary time and dead zones heavily corrupt policy training. Continuous learning at home creates severe sampling bias, causing the model to mistake a single static environment for a general pattern.

## Pwngrid Integration
The network reporting system has been overhauled to drastically improve user experience and performance:
* **Modern API Backend**: The original grid API has been replaced with the modern `opwngrid.xyz` platform.
* **Under-the-Hood Overhaul**: The core plugin architecture, internal reporting server, and binary components have been completely re-engineered.
* **Process Isolation & Smart Caching**: Data processing is now decoupled from the main loops. This architectural separation and aggressive caching eliminate the legacy system lag and idle bugs that previously caused freezes or slow performance at the beginning of a new epoch.

## Plugins
Countless fixes have been implemented across the entire plugin ecosystem. Since it is impossible to list every tweak, you can check full details in the [main image repository](https://github.com/NixPokeFun/pwnagotchi32-sb/) or browse comprehensive descriptions in the [dedicated plugins repository](https://github.com/NixPokeFun/pwnagotchi32-plugins/).

### Bluetooth Tethering & Status UI
The ultra-lightweight `bt-pan` script manages background Bluetooth NAP auto-connections via `bluetoothctl` (perfect for handling dynamic phone IPs like the Pixel 10 Pro), while the `control` plugin aggregates these metrics to map them straight onto your display. 

1. **Pair your phone via CLI:**
   ```bash
   sudo bluetoothctl
   scan on
   pair XX:XX:XX:XX:XX:XX
   trust XX:XX:XX:XX:XX:XX
   exit
   ```
2. **Enable both modules in `/etc/pwnagotchi/config.toml`:**
   ```toml
   [main.plugins.bt-pan]
   enabled = true
   dhcp = true
   interval = 60

   [main.plugins.control]
   enabled = true
   iface = true
   iface_pos = "5,32"
   blinds = true
   blinds_pos = "50,32"
   wan = true
   wan_pos = "100,0"
   ip = true
   ip_pos = "0,96"
   net_check_interval = 30
   ```

When connected, the `control` UI automatically updates the status line to `WAN: BT` and displays the dynamic Bluetooth IP address. This allows you to immediately access the Pwnagotchi Web UI at port `8080` right from your phone's browser on the move.

## System Resilience & SD Protection

The OS includes built-in safeguards to prevent data loss and protect physical hardware during unexpected shutdowns or software failures:

- **Graceful State Persistence** (`SIGTERM`): Upon receiving a termination signal, the process instantly triggers an emergency save. The active weights and configuration states (`brain.nn` and `brain.json`) are immediately flushed to non-volatile memory to prevent loss of epoch progress.
- **2048 MB Swap Allocation**: A dedicated 2048 MB swap file has been pre-configured to prevent system freezes and out-of-memory kernel panics during heavy neural network training, adding a critical layer of stability alongside other SD card protections.
- **Watchdog Protection & SD Wear Reduction**: The system tracks hardware and software restarts over short intervals. If an excessive loop of watchdog reboots is detected, it automatically initiates protective protocols - throttling aggressive IO operations and switching dangerous write buffers to prevent corruption and prolong the lifespan of your SD card.
  
## Troubleshooting & Diagnostics

If your Pwnagotchi isn't behaving as expected or fails to boot, follow these diagnostic steps to identify the issue before opening an issue or submitting a bug report:

1. **Verify Connectivity & Interfaces**:
   - Check DNS and internet routing:
     ```bash
     ping -c 3 8.8.8.8
     ```
   - Verify wireless interface state and active monitors:
     ```bash
     iw dev
     ```

2. **Inspect System Logs**:
   - View live output or log history:
     ```bash
     pwnylog
     # or
     cat /var/log/pwnagotchi.log
     ```

3. **Debug Early Boot Crashes**:
   - If the system crashes *before* the service fully starts, standard log files will often remain empty. Run the process manually in debug mode to expose deep boot traces:
     ```bash
     sudo pwnagotchi --debug
     ```

4. **Recovering from AI Crashes**:
   - If the AI engine fails to initialize or freezes, check the neural network files stored in `/root/` (`brain.nn` and `brain.json`).
   - Restore the auto-saved backup generated by `SIGTERM`, or remove the corrupted brain files entirely to allow the system to generate fresh ones on the next boot:
     ```bash
     sudo rm /root/brain.nn /root/brain.json
     ```
   - Rerun `sudo pwnagotchi --debug` to verify if the AI engine loads properly.

## System Architecture & Hardware Updates

- **Core OS & Dependency Engine**: The base system has been fully migrated to a newer Debian distribution release. A custom image builder utility is now integrated into the development workflow to handle environment provisioning and reproducible builds.
- **Optimized AI & Machine Learning Stack**: Core AI scripts and model runtime execution were heavily refactored for maximum efficiency across all operational scenarios. Neural network operations rely on custom-built wheel packages for `torch` and `torchvision`, optimized specifically for ARM architecture to minimize inference latency and runtime memory overhead.
- **Identity & Key Management**: Refactored internal architecture (`identity.py`, `faces.py`, `voice.py`) optimizes cryptographic key generation routines, expands the system status expression dataset, and isolates voice line handling.
- **Display Engine & Smart Layout Manager**: Extended native driver support to modern e-ink panels, including the Waveshare 2.13-inch V4 and decoupled the unit name and last captured handshake SSID into isolated UI widgets. The SSID widget enforces automatic character truncation to prevent overlay collisions on the status bar, while the layout system dynamically calculates font metrics to right-align operational mode labels longer than 4 characters, preventing text overflow.
- **Configuration & Security Control**: Added fine-grained control parameters in `/etc/pwnagotchi/config.toml` to manage display rendering and Web UI authentication (including `ui.invert`, `ui.cursor`, and `ui.web.auth`).

**Good luck with your training, and be careful! (♥‿‿♥)**
