import os, paramiko, time
from dataclasses import dataclass
from typing import Optional, Tuple, List
from dotenv import load_dotenv

ENV_PATH = r"C:\Users\NBR38\OneDrive - Sky\Documents\R&D\AI\ButlerSimulator\backend\.env"

print("[dotenv] Loading:", ENV_PATH)
load_dotenv(dotenv_path=ENV_PATH, override=True)
print("[dotenv] LOCAL_BOARD_IP =", os.getenv("LOCAL_BOARD_IP"))

# Predefined model configurations (paths are relative to RKLLM_BASE_PATH on the remote board)
MODEL_CONFIGS = {
    "Qwen2.5-1.5B-Instruct": {
        "dir": "Qwen2.5-1.5B-Instruct",
        "model": "Qwen2.5-1.5B-Instruct.rknn",
        "tokenizer": "Qwen2.5-1.5B-Instruct.tokenizer.gguf",
        "embedding": "Qwen2.5-1.5B-Instruct.embed.bin",
        "params": "--n_predict 4096 --repeat-penalty 1.1 --presence-penalty 1.0 --frequency-penalty 1.0 --top-k 1 --top-p 0.8 --temp 0.8 --jinja",
    },
    "Qwen2.5-3B-Instruct": {
        "dir": "Qwen2.5-3B-Instruct",
        "model": "Qwen2.5-3B-Instruct.rknn",
        "tokenizer": "Qwen2.5-3B-Instruct.tokenizer.gguf",
        "embedding": "Qwen2.5-3B-Instruct.embed.bin",
        "params": "--n_predict 4096 --repeat-penalty 1.1 --presence-penalty 1.0 --frequency-penalty 1.0 --top-k 1 --top-p 0.8 --temp 0.8 --jinja",
    },
    "Qwen2.5-7B-Instruct": {
        "dir": "Qwen2.5-7B-Instruct",
        "model": "Qwen2.5-7B-Instruct.rknn",
        "tokenizer": "Qwen2.5-7B-Instruct.tokenizer.gguf",
        "embedding": "Qwen2.5-7B-Instruct.embed.bin",
        "params": "--n_predict 4096 --repeat-penalty 1.1 --presence-penalty 1.0 --frequency-penalty 1.0 --top-k 1 --top-p 0.8 --temp 0.8 --jinja",
    },
}

@dataclass
class SSHConfig:
    host: str
    port: int
    user: str
    password: str
    timeout: int = 10

class RKLLMManager:
    """
    Minimal, manual-IP version:
    - One board
    - One SSH connection
    - No auto-switching
    """

    def __init__(self):
        self.cfg = SSHConfig(
            host=os.getenv("LOCAL_BOARD_IP"),
            port=int(os.getenv("LOCAL_BOARD_SSH_PORT", "22")),
            user=os.getenv("LOCAL_BOARD_USER", "root"),
            password=os.getenv("LOCAL_BOARD_PASSWORD", ""),
            timeout=int(os.getenv("LOCAL_BOARD_SSH_TIMEOUT", "10")),
        )
        self._client: Optional[paramiko.SSHClient] = None


    def connect(self) -> None:
        if self._client:
            transport = self._client.get_transport()
            if transport and transport.is_active():
                return
            self.close()

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self.cfg.host,
            port=self.cfg.port,
            username=self.cfg.user,
            password=self.cfg.password,
            timeout=self.cfg.timeout,
        )
        self._client = client

    def exec(self, command: str, timeout: int = 10) -> Tuple[str, str, int]:
        self.connect()
        assert self._client is not None

        stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="ignore").strip()
        err = stderr.read().decode("utf-8", errors="ignore").strip()
        return out, err, exit_code

    def ping(self) -> bool:
        out, _, code = self.exec("echo ping", timeout=3)
        return code == 0 and "ping" in out

    def close(self) -> None:
        if self._client:
            try:
                self._client.close()
            finally:
                self._client = None
    
    
    def exec_bg(self, command: str) -> int:
        """
        Run a long command in the background on the board and return the PID.
        """
        # Wrap it with nohup and return PID immediately
        wrapped = f"nohup sh -c '{command}' > /tmp/rkllm_server.log 2>&1 & echo $!"
        out, err, code = self.exec(wrapped, timeout=10)

        if code != 0:
            raise RuntimeError(f"Failed to start background command. code={code}, err={err}, out={out}")

        pid_str = out.strip().splitlines()[0].strip()
        if not pid_str.isdigit():
            raise RuntimeError(f"Did not receive a valid PID. out='{out}', err='{err}'")

        return int(pid_str)

    def start_model(self, model_name: str) -> Tuple[int, str, str]:
        """Start a RKLLM server for a named model from MODEL_CONFIGS.

        Returns a tuple (pid, command, model_file)
        """
        if model_name not in MODEL_CONFIGS:
            raise RuntimeError(f"Unknown model: {model_name}")

        cfg = MODEL_CONFIGS[model_name]
        base_path = os.getenv("RKLLM_BASE_PATH", "/home/firefly/llm")
        rk_port = os.getenv("RKLLM_PORT", "8080")
        ssh_host = self.cfg.host

        model_dir = f"{base_path}/{cfg['dir']}"
        model_file = cfg["model"]
        tokenizer = cfg["tokenizer"]
        embedding = cfg["embedding"]
        params = cfg.get("params", "")

        cmd = (
            f"cd {model_dir} && "
            f"rkllm3-server "
            f"-m {model_file} "
            f"--vocab {tokenizer} "
            f"--embedding {embedding} "
            f"--host {ssh_host} "
            f"--port {rk_port} "
            f"{params} "
            f"> /tmp/rkllm_server.log 2>&1 & echo $!"
        )

        out, err, code = self.exec(cmd, timeout=10)
        if code != 0:
            raise RuntimeError(f"Failed to start model server. code={code}, out={out}, err={err}")

        pid_str = out.strip().splitlines()[0].strip()
        if not pid_str.isdigit():
            raise RuntimeError(f"Did not receive PID when starting model. out='{out}', err='{err}'")

        return int(pid_str), cmd, model_file

    def is_pid_running(self, pid: int) -> bool:
        out, err, code = self.exec(f"ps -p {pid} -o comm=", timeout=3)
        return code == 0 and bool(out.strip())

    def tail_log(self, lines: int = 30) -> str:
        out, _, _ = self.exec(f"tail -{lines} /tmp/rkllm_server.log", timeout=3)
        return out
    
    
    def find_rkllm_pids(self) -> List[int]:
        """
        Return PIDs of any rkllm3-server processes on the board.
        Uses pgrep -x to match the exact process name.
        """
        out, err, code = self.exec("pgrep -x rkllm3-server || true", timeout=5)
        pids = []
        for line in out.splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
        return pids

    def kill_pids(self, pids: List[int], sig: int = 15) -> None:
        """
        Send a signal to a list of PIDs (default SIGTERM=15).
        """
        if not pids:
            return
        pid_str = " ".join(str(p) for p in pids)
        # Use 'kill -<sig>' for portability
        self.exec(f"kill -{sig} {pid_str} || true", timeout=5)

    def cleanup_rkllm_servers(self, grace_seconds: float = 1.5) -> dict:
        """
        Kill any existing rkllm3-server processes.
        1) SIGTERM
        2) wait
        3) SIGKILL anything still alive
        Returns a small report dict for debugging.
        """
        before = self.find_rkllm_pids()
        if not before:
            return {"killed": [], "remaining": [], "note": "no existing servers"}

        # Try graceful stop first (SIGTERM)
        self.kill_pids(before, sig=15)
        time.sleep(grace_seconds)

        remaining = self.find_rkllm_pids()
        if remaining:
            # Force kill (SIGKILL)
            self.kill_pids(remaining, sig=9)
            time.sleep(0.2)

        after = self.find_rkllm_pids()
        killed = [p for p in before if p not in after]

        return {"killed": killed, "remaining": after, "note": "cleanup attempted"}

