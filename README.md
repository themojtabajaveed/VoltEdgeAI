# VoltEdgeAI

## Deploying on Google Cloud VM

**Prerequisites:**
- Ubuntu VM with static IP and SSH access.
- Zerodha API keys and access token.

**Step-by-Step Deployment:**

1. **SSH into the VM:**
   ```bash
   ssh mujtaba12cr@34.100.190.223
   ```

2. **Clone the repository:**
   ```bash
   git clone <your-repo-url> ~/voltedge
   ```

3. **Run the server setup script:**
   This strictly initializes the `python3 -m venv` and locally resolves `requirements.txt`.
   ```bash
   cd ~/voltedge
   chmod +x scripts/setup_server.sh
   ./scripts/setup_server.sh
   ```

4. **Configure Environment Variables:**
   Create `.env` natively at `~/voltedge/.env` and securely populate your API configs. 
   **CRITICAL:** Set `VOLTEDGE_LIVE_MODE=0` for the initial deployment to safely lock the execution to DRY_RUN mode only.
   ```bash
   nano .env
   ```

5. **Install and Start the Systemd Service:**
   This mounts the daemon directly into the Ubuntu lifecycle.
   ```bash
   cd ~/voltedge
   chmod +x scripts/install_service.sh
   sudo ./scripts/install_service.sh mujtaba12cr
   ```

6. **Verify the Service:**
   ```bash
   sudo systemctl status voltedge.service
   ```

7. **Monitor the Logs:**
   To follow raw application traces in real-time, tail the `journalctl`:
   ```bash
   journalctl -u voltedge.service -f
   ```
