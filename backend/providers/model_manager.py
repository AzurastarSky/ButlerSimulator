"""
Remote RKLLM Model Manager (adapted from CloudLocalBattle project)

Manages lifecycle of rkllm3-server processes on a remote board via SSH.
Ensures only one model runs at a time and handles cleanup on disconnect.
"""

import os
import time
import threading
import atexit
from typing import Optional, Dict, Any
import paramiko
from dotenv import load_dotenv

load_dotenv()

# ============================================================================
# CONFIGURATION
# ============================================================================

SSH_HOST = os.getenv("LOCAL_BOARD_IP", "192.168.0.222")
SSH_PORT = int(os.getenv("LOCAL_BOARD_SSH_PORT", "22"))
SSH_USER = os.getenv("LOCAL_BOARD_USER", "root")
SSH_PASSWORD = os.getenv("LOCAL_BOARD_PASSWORD", "")

RKLLM_PORT = int(os.getenv("LOCAL_BOARD_PORT", "8080"))
RKLLM_BASE_PATH = os.getenv("RKLLM_BASE_PATH", "/home/firefly/llm")

MODEL_CONFIGS: Dict[str, Dict[str, Any]] = {
    "Qwen2.5-1.5B-Instruct": {
        "dir": "Qwen2.5-1.5B-Instruct",
        "model": "Qwen2.5-1.5B-Instruct.rknn",
        "tokenizer": "Qwen2.5-1.5B-Instruct.tokenizer.gguf",
        "embedding": "Qwen2.5-1.5B-Instruct.embed.bin",
        "params": "--n_predict 4096 --repeat-penalty 1.1 --presence-penalty 1.0 --frequency-penalty 1.0 --top-k 1 --top-p 0.8 --temp 0.8 --jinja"
    },
    "Qwen2.5-3B-Instruct": {
        "dir": "Qwen2.5-3B-Instruct",
        "model": "Qwen2.5-3B-Instruct.rknn",
        "tokenizer": "Qwen2.5-3B-Instruct.tokenizer.gguf",
        "embedding": "Qwen2.5-3B-Instruct.embed.bin",
        "params": "--n_predict 4096 --repeat-penalty 1.1 --presence-penalty 1.0 --frequency-penalty 1.0 --top-k 1 --top-p 0.8 --temp 0.8 --jinja"
    },
    "Qwen2.5-7B-Instruct": {
        "dir": "Qwen2.5-7B-Instruct",
        "model": "Qwen2.5-7B-Instruct.rknn",
        "tokenizer": "Qwen2.5-7B-Instruct.tokenizer.gguf",
        "embedding": "Qwen2.5-7B-Instruct.embed.bin",
        "params": "--n_predict 4096 --repeat-penalty 1.1 --presence-penalty 1.0 --frequency-penalty 1.0 --top-k 1 --top-p 0.8 --temp 0.8 --jinja"
    },
}


class RemoteModelManager:
    def __init__(self):
        self.ssh_client: Optional[paramiko.SSHClient] = None
        self.current_model: Optional[str] = None
        self.desired_model: Optional[str] = None
        self.server_pid: Optional[int] = None
        self.last_error: Optional[str] = None
        self.lock = threading.Lock()
        atexit.register(self.cleanup_all)

    def _get_ssh_client(self) -> paramiko.SSHClient:
        if self.ssh_client is not None:
            try:
                transport = self.ssh_client.get_transport()
                if transport and transport.is_active():
                    return self.ssh_client
            except:
                pass

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        # Attempt connection. If password is empty, try key-based auth (from env path or default).
        key_path = os.getenv("LOCAL_BOARD_SSH_KEY_PATH", "").strip()
        pkey = None
        if not SSH_PASSWORD:
            # Try to load a private key if provided or from default locations
            candidates = []
            if key_path:
                candidates.append(os.path.expanduser(key_path))
            # common defaults
            candidates.extend([os.path.expanduser("~/.ssh/id_rsa"), os.path.expanduser("~/.ssh/id_ed25519")])
            for kp in candidates:
                try:
                    if os.path.exists(kp):
                        try:
                            pkey = paramiko.Ed25519Key.from_private_key_file(kp)
                        except Exception:
                            try:
                                pkey = paramiko.RSAKey.from_private_key_file(kp)
                            except Exception:
                                pkey = None
                        if pkey:
                            break
                except Exception:
                    pkey = None

        try:
            connect_kwargs = dict(hostname=SSH_HOST, port=SSH_PORT, username=SSH_USER, timeout=10)
            if SSH_PASSWORD:
                connect_kwargs["password"] = SSH_PASSWORD
            if pkey is not None:
                connect_kwargs["pkey"] = pkey
            # allow agent and look_for_keys to use ssh-agent if available
            connect_kwargs["allow_agent"] = True
            connect_kwargs["look_for_keys"] = True

            client.connect(**connect_kwargs)
            self.ssh_client = client
            print(f"[ModelManager] SSH connected to {SSH_HOST}:{SSH_PORT} as {SSH_USER}")
            return client
        except Exception as e:
            # Provide helpful debug info
            err_msg = f"SSH connection failed to {SSH_HOST}:{SSH_PORT} as {SSH_USER}: {e}"
            print(f"[ModelManager] {err_msg}")
            raise ConnectionError(err_msg)

    def _exec_command(self, command: str, timeout: int = 10) -> tuple[str, str, int]:
        client = self._get_ssh_client()
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode('utf-8', errors='ignore').strip()
        err = stderr.read().decode('utf-8', errors='ignore').strip()
        return out, err, exit_code

    def _exec_background_command(self, command: str) -> str:
        """Execute a command in the background via SSH and return its PID.
        
        The command will be started with nohup and output redirected to /tmp/rkllm_server.log.
        Returns the PID as a string.
        """
        client = self._get_ssh_client()
        # Create a simple wrapper script that we execute
        # This avoids complex shell quoting issues
        wrapped_cmd = f"nohup {command} > /tmp/rkllm_server.log 2>&1 & echo $!"
        stdin, stdout, stderr = client.exec_command(wrapped_cmd, get_pty=False)
        start = time.time()
        pid_output = ""
        err_output = ""
        while time.time() - start < 5:
            if stdout.channel.recv_ready():
                chunk = stdout.read(1024).decode('utf-8', errors='ignore')
                pid_output += chunk
                if '\n' in pid_output:
                    break
            if stderr.channel.recv_stderr_ready():
                err_output += stderr.read(1024).decode('utf-8', errors='ignore')
            time.sleep(0.1)
        pid = pid_output.strip().split('\n')[0].strip()
        if err_output:
            print(f"[ModelManager] SSH stderr during start: {err_output}")
        return pid

    def _kill_existing_server(self):
        try:
            out, _, _ = self._exec_command("pgrep -f rkllm3-server")
            pids = [pid.strip() for pid in out.split('\n') if pid.strip()]
            if pids:
                for pid in pids:
                    self._exec_command(f"kill -15 {pid}")
                    time.sleep(0.5)
                time.sleep(1)
                out, _, _ = self._exec_command("pgrep -f rkllm3-server")
                remaining = [pid.strip() for pid in out.split('\n') if pid.strip()]
                if remaining:
                    for pid in remaining:
                        self._exec_command(f"kill -9 {pid}")
        except Exception as e:
            print(f"[ModelManager] Error during cleanup: {e}")

    def start_model(self, model_name: str) -> bool:
        with self.lock:
            if model_name not in MODEL_CONFIGS:
                print(f"[ModelManager] Unknown model: {model_name}")
                return False
            if self.current_model == model_name and self.server_pid:
                try:
                    proc_check, _, proc_code = self._exec_command(f"ps -p {self.server_pid} -o comm=", timeout=3)
                    if proc_code == 0 and proc_check:
                        print(f"[ModelManager] Model {model_name} already running (PID: {self.server_pid})")
                        return True
                except Exception:
                    pass

            try:
                self._kill_existing_server()
                self.current_model = None
                self.server_pid = None
                config = MODEL_CONFIGS[model_name]
                model_dir = f"{RKLLM_BASE_PATH}/{config['dir']}"
                # Use full paths so we don't need cd (which doesn't work well with nohup)
                cmd = (
                    f"rkllm3-server "
                    f"-m {model_dir}/{config['model']} "
                    f"--vocab {model_dir}/{config['tokenizer']} "
                    f"--embedding {model_dir}/{config['embedding']} "
                    f"--host 0.0.0.0 "
                    f"--port {RKLLM_PORT} "
                    f"{config['params']}"
                )
                pid_str = self._exec_background_command(cmd)
                if not pid_str or not pid_str.isdigit():
                    dir_check, _, _ = self._exec_command(f"test -d {model_dir} && echo 'exists'", timeout=3)
                    if "exists" not in dir_check:
                        print(f"[ModelManager] ERROR: Directory does not exist: {model_dir}")
                        return False
                    return False
                try:
                    self.server_pid = int(pid_str)
                except:
                    return False
                time.sleep(1)
                proc_check, _, proc_code = self._exec_command(f"ps -p {self.server_pid} -o comm=", timeout=3)
                if proc_code != 0 or not proc_check:
                    log_out, _, _ = self._exec_command("tail -10 /tmp/rkllm_server.log", timeout=3)
                    print(f"[ModelManager] Server log:\n{log_out}")
                    return False
                for i in range(180):
                    time.sleep(0.5)
                    if i > 4:
                        log_check, _, log_code = self._exec_command("grep 'server is listening' /tmp/rkllm_server.log", timeout=3)
                        if log_code == 0 and log_check:
                            self.current_model = model_name
                            return True
                    out, _, code = self._exec_command(f"netstat -tuln | grep :{RKLLM_PORT}", timeout=3)
                    if code == 0 and str(RKLLM_PORT) in out:
                        self.current_model = model_name
                        return True
                log_out, _, _ = self._exec_command("tail -20 /tmp/rkllm_server.log", timeout=3)
                print("[ModelManager] Server did not start in time")
                print(f"[ModelManager] Server log:\n{log_out}")
                self._kill_existing_server()
                return False
            except Exception as e:
                print(f"[ModelManager] Error starting model: {e}")
                self._kill_existing_server()
                return False

    # Warmup behavior removed: model start no longer performs background warmup requests.

    def stop_current_model(self):
        with self.lock:
            if self.current_model:
                self._kill_existing_server()
                self.current_model = None
                self.server_pid = None

    def get_current_model(self) -> Optional[str]:
        return self.current_model

    def check_board_health(self) -> bool:
        try:
            out, err, code = self._exec_command("echo 'ping'", timeout=3)
            if code == 0 and 'ping' in out:
                if self.desired_model and not self.current_model:
                    self.last_error = None
                    return True
                if self.current_model and self.server_pid:
                    proc_check, _, proc_code = self._exec_command(f"ps -p {self.server_pid}", timeout=3)
                    if proc_code != 0:
                        self.current_model = None
                        self.server_pid = None
                        self.last_error = None
                        return True
                self.last_error = None
                return True
            else:
                self.last_error = "Board not responding"
                self.current_model = None
                self.server_pid = None
                self.ssh_client = None
                return False
        except ConnectionError as e:
            self.last_error = f"Board not available: {str(e)}"
            self.current_model = None
            self.server_pid = None
            self.ssh_client = None
            return False
        except Exception as e:
            error_str = str(e).lower()
            if 'timeout' in error_str or 'connection' in error_str:
                self.last_error = "Board connection lost"
            else:
                self.last_error = f"Board error: {str(e)}"
            self.current_model = None
            self.server_pid = None
            self.ssh_client = None
            return False

    def start_model_async(self, model_name: str):
        def _start():
            try:
                self.last_error = None
                self.desired_model = model_name
                success = self.start_model(model_name)
                if not success:
                    self.last_error = "Model failed to start"
            except ConnectionError as e:
                print(f"[ModelManager] SSH connection failed: {e}")
                self.last_error = f"Board not available: {str(e)}"
            except Exception as e:
                print(f"[ModelManager] Async start failed: {e}")
                self.last_error = f"Error: {str(e)}"
        thread = threading.Thread(target=_start, daemon=True)
        thread.start()

    def cleanup_all(self):
        try:
            self._kill_existing_server()
        except:
            pass
        if self.ssh_client:
            try:
                self.ssh_client.close()
            except:
                pass
            self.ssh_client = None
        self.current_model = None
        self.server_pid = None


# Singleton instance
_manager: Optional[RemoteModelManager] = None

def get_manager() -> RemoteModelManager:
    global _manager
    if _manager is None:
        _manager = RemoteModelManager()
    return _manager

def get_available_models() -> list[str]:
    return list(MODEL_CONFIGS.keys())
