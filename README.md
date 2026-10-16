# Blockyard — Minecraft Server Manager

A Windows-first local manager for Minecraft Java Edition. Search Modrinth modpacks, convert supported client modpacks into server folders, or import a complete server-pack ZIP, then configure and control the server in a browser UI.

## Start the app

1. Install Python 3.10 or newer.
2. Install a Java runtime compatible with your modpack’s Minecraft version.
3. Double-click `run.bat`. Blockyard opens at `http://127.0.0.1:8765`.
4. Open **Discover packs** and follow the guided installer. Choose **Browse Modrinth**, **Browse CurseForge**, or **Import a pack**. CurseForge search and installs require an API key that is authorized for your use; add it in **Settings**. Set the server name and folder, then let Blockyard prepare it.
5. When setup finishes, review **Properties**, accept the Minecraft EULA if you agree to it, then press **Start**.

The default install folder is `Minecraft Servers` under your Windows user profile. Each server gets its own folder inside it. You can change the default in Settings or choose a different folder for an individual server. Blockyard remembers the most recent install location as the default for the next install.

Use **Close Blockyard** in the left sidebar to shut down the manager when you are done. It asks for confirmation, then gracefully stops managed Minecraft servers and closes the local manager. Starting `run.bat` while Blockyard is already running will not start a second manager.

## What it does

- Searches the Modrinth Minecraft modpack catalog and lists available `.mrpack` releases.
- Searches CurseForge’s official Minecraft modpack catalog and installs a selected release. It uses the author’s server pack when available; otherwise it resolves and downloads the profile’s required mod files through CurseForge’s API.
- Installs Modrinth `.mrpack` files, shared and server-only overrides, server-supported files, and the declared Minecraft loader.
- Converts imported Modrinth `.mrpack` client packs, full CurseForge instance exports, and supported CurseForge or Prism/MultiMC client profile ZIPs into dedicated server folders by installing the declared loader and retaining shared configs/mods.
- Imports complete author-provided dedicated server-pack ZIPs directly from your computer.
- Checks mod metadata, startup crash output, and high-confidence rules from the [curated client-side mod list](https://github.com/LianJordaan/client-side-mods-list). The public list is cached in `%APPDATA%\Blockyard\client-only-list-cache.json`; Blockyard refreshes it at startup and when importing packs if the cache is older than six hours. It also records identified mod IDs, normalized filenames, and SHA-256 fingerprints in `%APPDATA%\Blockyard\client-only-mods.json` to improve future pack detection.
- Shows the full path of the learned client-only manifest in **Settings** and lets you edit its JSON there. Saving validates the structure and fingerprints before replacing the file; invalid JSON leaves the saved manifest untouched.
- Shows likely client-only mods in each server's **Mod compatibility** tab and waits for confirmation before moving anything. Curated rules are matched for the Minecraft version and loader; optional, heuristic, unknown, and protected library entries are not treated as removal candidates. Confirm one mod at a time or use **Confirm all**; confirmed JARs are kept under `blockyard-client-only-mods/` inside that server folder for recovery.
- Provides an **Undo remove** action for each moved-aside mod, restoring its JAR to the original `mods` path without overwriting an existing file.
- Shows compatibility results and any matching server releases in each server's **Mod compatibility** tab. Alternatives are provided for review and are not swapped into a pack automatically.
- Supports vanilla, Forge, NeoForge, Fabric, and Quilt loader setup.
- Saves game mode, difficulty, player count, view and simulation distance, access rules, MOTD, and server port to `server.properties`.
- Shows live server output, copies console output with one click, and accepts server console commands.
- Includes a server-folder file manager to browse, upload, download, delete, and edit text files, with paths restricted to each server folder.
- Detects existing server installations in the configured server directory and adds a selected folder to Blockyard without changing its files.
- Provides Start, Stop, Restart, and Force stop controls. Stop sends the server’s graceful `stop` command; Force stop terminates the process if it is unresponsive. Start stays unavailable until setup is complete and the Minecraft EULA choice is saved.
- Finds Java runtimes already installed on the host using `PATH`, `JAVA_HOME`/`JDK_HOME`, common install folders, Windows registry entries, and Minecraft Launcher runtimes. At install and launch time, Blockyard selects a detected runtime compatible with the server's Minecraft version (for example, Java 21 for Minecraft 1.21.x), even if Java on `PATH` is older. Choose among detected Java versions in **Properties**, enter a custom executable path, or use **Find Java** to rescan the host.
- Lets you remove a server from Blockyard while keeping its folder, or permanently delete its server folder from a confirmation dialog.
- Lets you choose an accent color in **Settings**, previews the selection immediately, and saves it for later launches.

## Notes

- Modrinth search and downloads do not require an account token. Blockyard checks the `.mrpack` server environment flags and the versions linked from its file metadata, then installs files marked compatible with dedicated servers.
- Blockyard downloads the public curated mod-rule list over HTTPS and caches it locally. It sends no server folder names or mod inventory to that list service. If the list is unavailable, Blockyard continues using jar metadata and its local learned manifest; it never moves a JAR without your confirmation.
- Importing Modrinth `.mrpack` files and supported CurseForge/Prism profile ZIPs does not require Docker, Podman, or a separate container runtime.
- Standard CurseForge profile exports commonly omit downloaded mod JARs. The local archive importer converts those archives only when the mod files are present in the ZIP. For catalog installs, **Browse CurseForge** uses the official API to resolve missing required files. Full CurseForge instance exports with local mod JARs are also supported, including exports nested under a named folder. Client-only folders and launcher metadata are left out of the server folder.
- A CurseForge API key is saved only in this computer’s `%APPDATA%\Blockyard\config.json`; the browser never receives its value. Blockyard sends it to CurseForge for authenticated API requests. Do not distribute or share an API key: CurseForge’s API terms say keys are non-transferable and must not be shared. Each user or installation must use a key authorized for that use. Review [CurseForge’s API terms](https://support.curseforge.com/support/solutions/articles/9000207405-curse-forge-3rd-party-api-terms-and-conditions) before applying for or using a key.
- If an imported archive omits its Minecraft version or loader metadata, enter those details in the installer. Forge and NeoForge need the exact loader version; Fabric and Quilt can resolve the latest compatible loader when no version is entered.
- Prism/MultiMC profile ZIPs use `mmc-pack.json` metadata when available; the installer can fill in missing version or loader details manually. The ZIP must include its mod JARs.
- A Java runtime compatible with the selected Minecraft version must be installed to start a native Modrinth/imported server. Blockyard searches common host locations automatically and reports when it finds only an incompatible version. If none is found, install Java or enter its full executable path in **Properties → Java executable**. The Find Java button only detects existing files; it does not download or install Java. Windows launch scripts with a UTF-8 byte-order mark are normalized before launch so `cmd.exe` can read the first command correctly.
- The manager page listens only on `127.0.0.1`. Game-server internet access is controlled separately by the Minecraft port and router/firewall settings.
- Minecraft port forwarding and firewall access must still be configured separately if players outside the local network need to join the game server.

Server settings are stored under `%APPDATA%\Blockyard`. Minecraft server files are stored in the server folders you choose.
