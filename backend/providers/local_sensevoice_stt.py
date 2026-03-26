"""
Local SenseVoice STT Provider (CloudLocalBattle style)
Uploads audio via SFTP to the target board and then calls the remote
SenseVoice `/transcribe` endpoint with a JSON payload containing the
remote file path. This matches the original CloudLocalBattle behaviour.
"""

import os
import requests
import paramiko
import time
import subprocess
import threading
from typing import Optional


# Configuration: SenseVoice STT server
# Default to user's home board and port 4500
LOCAL_BOARD_IP = os.getenv("LOCAL_BOARD_IP", "192.168.1.245")

SENSEVOICE_HOST = os.getenv("SENSEVOICE_HOST", LOCAL_BOARD_IP)
SENSEVOICE_PORT = os.getenv("SENSEVOICE_PORT", "4500")
SENSEVOICE_BASE_URL = f"http://{SENSEVOICE_HOST}:{SENSEVOICE_PORT}"

# SSH configuration for file upload
SENSEVOICE_SSH_HOST = os.getenv("SENSEVOICE_SSH_HOST", LOCAL_BOARD_IP)
SENSEVOICE_SSH_PORT = int(os.getenv("SENSEVOICE_SSH_PORT", "22"))
SENSEVOICE_SSH_USER = os.getenv("SENSEVOICE_SSH_USER", "firefly")
SENSEVOICE_SSH_PASSWORD = os.getenv("SENSEVOICE_SSH_PASSWORD", "firefly")

# Remote path for temporary audio files
SENSEVOICE_TEMP_DIR = os.getenv("SENSEVOICE_TEMP_DIR", "/tmp/stt_uploads")

# Connection pool for SSH reuse (significant speedup)
_ssh_connection = None
_ssh_lock = threading.Lock()
_temp_dir_created = False


def _reset_ssh_connection():
    global _ssh_connection, _temp_dir_created
    with _ssh_lock:
        try:
            if _ssh_connection is not None:
                _ssh_connection.close()
        except Exception:
            pass
        _ssh_connection = None
        _temp_dir_created = False


def _get_ssh_connection():
    global _ssh_connection, _temp_dir_created
    if _ssh_connection is not None:
        try:
            transport = _ssh_connection.get_transport()
            if transport is not None and transport.is_active():
                return _ssh_connection
        except Exception:
            _reset_ssh_connection()

    with _ssh_lock:
        if _ssh_connection is not None:
            try:
                transport = _ssh_connection.get_transport()
                if transport is not None and transport.is_active():
                    return _ssh_connection
            except Exception:
                _reset_ssh_connection()

        ssh_client = paramiko.SSHClient()
        ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh_client.connect(
            hostname=SENSEVOICE_SSH_HOST,
            port=SENSEVOICE_SSH_PORT,
            username=SENSEVOICE_SSH_USER,
            password=SENSEVOICE_SSH_PASSWORD,
            timeout=10,
            allow_agent=False,
            look_for_keys=False,
        )

        # Enable TCP keepalive on the Paramiko transport to reduce long stalls
        try:
            transport = ssh_client.get_transport()
            if transport is not None:
                transport.set_keepalive(30)
        except Exception:
            pass

        try:
            print(f"[local_sensevoice_stt] SSH connection established to {SENSEVOICE_SSH_HOST}:{SENSEVOICE_SSH_PORT}")
        except Exception:
            pass

        if not _temp_dir_created:
            try:
                stdin, stdout, stderr = ssh_client.exec_command(f"mkdir -p {SENSEVOICE_TEMP_DIR}")
                stdout.channel.recv_exit_status()
            except Exception:
                pass
            _temp_dir_created = True

        _ssh_connection = ssh_client
        return ssh_client


def _cleanup_remote_file_async(filepath: str):
    def cleanup():
        try:
            ssh = _get_ssh_connection()
            ssh.exec_command(f"rm -f {filepath}")
        except Exception:
            pass
    threading.Thread(target=cleanup, daemon=True).start()


def transcribe_audio(audio_bytes: bytes, language: str = "en"):
    remote_path = None
    try:
        timestamp = int(time.time() * 1000)
        filename = f"audio_{timestamp}.wav"
        remote_path = f"{SENSEVOICE_TEMP_DIR}/{filename}"

        is_wav = audio_bytes[:4] == b'RIFF' and audio_bytes[8:12] == b'WAVE'
        if is_wav:
            wav_bytes = audio_bytes
            convert_ms = 0.0
        else:
            t0 = time.perf_counter()
            try:
                ffmpeg_result = subprocess.run([
                    "ffmpeg", "-loglevel", "error", "-i", "pipe:0",
                    "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", "-f", "wav", "pipe:1"
                ], input=audio_bytes, capture_output=True, timeout=10)
                if ffmpeg_result.returncode != 0:
                    raise Exception(ffmpeg_result.stderr.decode('utf-8', errors='replace')[:400])
                wav_bytes = ffmpeg_result.stdout
            except FileNotFoundError:
                raise Exception("ffmpeg not installed on server")
            convert_ms = (time.perf_counter() - t0) * 1000

        # upload via sftp
        ssh = _get_ssh_connection()
        t_upload_start = time.perf_counter()
        sftp = ssh.open_sftp()
        try:
            with sftp.file(remote_path, 'wb') as rf:
                rf.write(wav_bytes)
        finally:
            sftp.close()
        upload_ms = (time.perf_counter() - t_upload_start) * 1000

        # request transcription by path
        t_inf_start = time.perf_counter()
        endpoint = f"{SENSEVOICE_BASE_URL}/transcribe"
        payload = {"audio_file_path": remote_path, "language": language, "use_itn": False}
        resp = requests.post(endpoint, json=payload, headers={"Content-Type": "application/json"}, timeout=30)
        t_inf_end = time.perf_counter()
        inference_ms = (t_inf_end - t_inf_start) * 1000

        if resp.status_code != 200:
            raise Exception(f"SenseVoice returned {resp.status_code}: {resp.text[:500]}")

        result = None
        try:
            result = resp.json()
        except Exception:
            # server returned non-JSON — treat as plain text
            result = {'text': resp.text}

        transcript = ''
        if isinstance(result, dict):
            transcript = result.get('text') or result.get('transcription') or result.get('result') or ''
        else:
            transcript = str(result)

        if not transcript or not transcript.strip():
            raise Exception(f"Empty transcription, server response: {result}")

        total_ms = convert_ms + upload_ms + inference_ms
        # cleanup remote file async
        _cleanup_remote_file_async(remote_path)
        return {"text": transcript.strip(), "inference_ms": inference_ms, "upload_ms": upload_ms, "convert_ms": convert_ms, "total_ms": total_ms}

    except Exception as e:
        raise Exception(f"SenseVoice STT transcription failed: {e}")


def warmup_connections() -> bool:
    try:
        _get_ssh_connection()
        return True
    except Exception:
        return False


def warmup_model() -> bool:
    try:
        # generate 1s silence wav
        sample_rate = 16000
        num_samples = sample_rate
        num_channels = 1
        bytes_per_sample = 2
        import struct
        wav_header = struct.pack('<4sI4s4sIHHIIHH4sI', b'RIFF', 36 + num_samples * num_channels * bytes_per_sample, b'WAVE', b'fmt ', 16, 1, num_channels, sample_rate, sample_rate * num_channels * bytes_per_sample, num_channels * bytes_per_sample, bytes_per_sample * 8, b'data', num_samples * num_channels * bytes_per_sample)
        silence = b'\x00' * (num_samples * num_channels * bytes_per_sample)
        dummy = wav_header + silence
        # upload and call transcribe briefly
        ssh = _get_ssh_connection()
        warmup_path = f"{SENSEVOICE_TEMP_DIR}/warmup_{int(time.time()*1000)}.wav"
        sftp = ssh.open_sftp()
        try:
            with sftp.file(warmup_path, 'wb') as rf:
                rf.write(dummy)
        finally:
            sftp.close()
        resp = requests.post(f"{SENSEVOICE_BASE_URL}/transcribe", json={"audio_file_path": warmup_path, "language": "en", "use_itn": False}, timeout=10)
        _cleanup_remote_file_async(warmup_path)
        return resp.status_code == 200
    except Exception:
        return False


def check_sensevoice_health() -> bool:
    try:
        r = requests.get(f"{SENSEVOICE_BASE_URL}/health", timeout=5)
        return r.status_code in (200, 404, 405)
    except Exception:
        try:
            r = requests.get(SENSEVOICE_BASE_URL, timeout=5)
            return r.status_code in (200, 404, 405)
        except Exception:
            return False
