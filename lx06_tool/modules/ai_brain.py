"""
AI brain installer for LX06 Flash Tool (Phase 3).

Handles:
- Soft Patch (Option A): Install xiaogpt to intercept Xiaoai's NLP
  and route responses through OpenAI/Gemini/Kimi LLMs.
  Uses the default Xiaomi wake word but overrides the thinking.
- Hard Patch (Option B): Install open-xiaoai Rust client to completely
  bypass Xiaomi servers, enabling custom local wake words and
  direct audio streaming to an AI server.

Both approaches are based on reference repositories:
- yihong0618/xiaogpt (soft patch)
- idootop/open-xiaoai (hard patch)

The installer injects the necessary binaries, configs, and init
scripts into the extracted rootfs.
"""

from __future__ import annotations

import logging
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from lx06_tool.config import CustomizationChoices
from lx06_tool.exceptions import FirmwareError
from lx06_tool.utils.compat import AsyncRunner

logger = logging.getLogger(__name__)


# ── Data Models ─────────────────────────────────────────────────────────────


@dataclass
class AIBrainResult:
    """Result of AI brain installation."""

    installed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    config_path: str = ""
    mode: str = ""  # "soft" or "hard"


# ── AI Brain Installer ──────────────────────────────────────────────────────


class AIBrainInstaller:
    """Installs AI brain components into the extracted LX06 rootfs.

    Supports two modes:
    - Soft Patch (xiaogpt): Keeps Xiaomi wake word, overrides LLM backend
    - Hard Patch (open-xiaoai): Replaces entire voice pipeline with Rust client

    Usage:
        installer = AIBrainInstaller(rootfs_dir=Path('./rootfs'))
        result = await installer.apply(choices=choices, on_output=callback)
    """

    def __init__(
        self,
        rootfs_dir: Path,
        binaries_dir: Path | None = None,
        runner: AsyncRunner | None = None,
    ):
        self._rootfs = rootfs_dir
        self._binaries_dir = binaries_dir or (
            Path(__file__).parent.parent.parent / "resources" / "binaries"
        )
        self._runner = runner or AsyncRunner(default_timeout=120.0, sudo=True)

    async def apply(
        self,
        choices: CustomizationChoices,
        *,
        on_output: Callable[[str, str], None] | None = None,
    ) -> AIBrainResult:
        """Apply AI brain installation based on user's mode selection.

        Args:
            choices: User's customization choices (must have ai_mode set).
            on_output: Callback for progress messages.

        Returns:
            AIBrainResult with installation details.
        """
        result = AIBrainResult(mode=choices.ai_mode)

        if choices.ai_mode == "none":
            if on_output:
                on_output("stdout", "  AI brain installation skipped.")
            return result

        if choices.ai_mode == "soft":
            await self._install_soft_patch(choices, result=result, on_output=on_output)
        elif choices.ai_mode == "hard":
            await self._install_hard_patch(choices, result=result, on_output=on_output)
        else:
            result.warnings.append(f"Unknown AI mode: {choices.ai_mode}")

        return result

    # ── Soft Patch (xiaogpt) ─────────────────────────────────────────────────

    async def _install_soft_patch(
        self,
        choices: CustomizationChoices,
        *,
        result: AIBrainResult,
        on_output: Callable[[str, str], None] | None = None,
    ) -> None:
        """Install xiaogpt soft patch.

        This approach:
        1. Keeps the stock Xiaomi voice engine running
        2. Intercepts the NLP response via Xiaomi's API
        3. Routes the query to an LLM (OpenAI/Gemini/Kimi)
        4. Plays back the LLM response through the speaker

        Requires:
        - Python 3 runtime in the rootfs (already present on LX06)
        - xiaogpt pip package or bundled script
        - API keys for the chosen LLM provider
        """
        if on_output:
            on_output("stdout", "  Installing xiaogpt (soft patch)...")

        # Step 1: Install xiaogpt script/package
        xiaogpt_dir = self._rootfs / "opt" / "xiaogpt"
        xiaogpt_dir.mkdir(parents=True, exist_ok=True)

        # Copy bundled xiaogpt files if available
        src_xiaogpt = self._binaries_dir / "xiaogpt"
        if src_xiaogpt.exists() and src_xiaogpt.is_dir():
            shutil.copytree(src_xiaogpt, xiaogpt_dir, dirs_exist_ok=True)
            result.installed.append("xiaogpt_scripts")
            logger.info("Copied xiaogpt scripts to %s", xiaogpt_dir)
        else:
            # Fallback: create a runner script that pip-installs on first boot
            runner_script = xiaogpt_dir / "install_and_run.sh"
            runner_script.write_text(
                "#!/bin/sh\n"
                "# xiaogpt installer - runs on first boot\n"
                "pip3 install xiaogpt 2>/dev/null || pip install xiaogpt 2>/dev/null\n"
                "exec python3 -m xiaogpt\n"
            )
            runner_script.chmod(0o755)
            result.warnings.append(
                "xiaogpt not bundled. Will attempt pip install on first boot "
                "(requires internet on the speaker)."
            )
            result.installed.append("xiaogpt_installer")

        # Step 2: Generate xiao_config.yaml
        config = self._generate_xiaogpt_config(choices)
        config_path = xiaogpt_dir / "xiao_config.yaml"
        config_path.write_text(config)
        result.config_path = str(config_path.relative_to(self._rootfs))
        result.installed.append("xiaogpt_config")
        logger.info("Generated xiao_config.yaml")

        # Step 3: Install init script
        init_script = self._rootfs / "etc" / "init.d" / "S90xiaogpt"
        init_script.parent.mkdir(parents=True, exist_ok=True)
        init_script.write_text(
            "#!/bin/sh\n"
            "# xiaogpt AI brain (soft patch)\n"
            "# Installed by LX06 Flash Tool\n"
            "\n"
            "DAEMON_DIR=/opt/xiaogpt\n"
            "CONFIG=$DAEMON_DIR/xiao_config.yaml\n"
            "PIDFILE=/var/run/xiaogpt.pid\n"
            "\n"
            "case \"$1\" in\n"
            "    start)\n"
            "        echo \"Starting xiaogpt...\"\n"
            "        cd $DAEMON_DIR\n"
            "        start-stop-daemon -S -b -m -p $PIDFILE \\\n"
            "            -x /bin/sh -- -c \"python3 -m xiaogpt --config $CONFIG\"\n"
            "        ;;\n"
            "    stop)\n"
            "        echo \"Stopping xiaogpt...\"\n"
            "        start-stop-daemon -K -p $PIDFILE\n"
            "        rm -f $PIDFILE\n"
            "        ;;\n"
            "    restart)\n"
            "        $0 stop\n"
            "        sleep 2\n"
            "        $0 start\n"
            "        ;;\n"
            "    *)\n"
            "        echo \"Usage: $0 {start|stop|restart}\"\n"
            "        exit 1\n"
            "        ;;\n"
            "esac\n"
        )
        init_script.chmod(0o755)
        result.installed.append("xiaogpt_init")

        if on_output:
            on_output(
                "stdout",
                f"  ✅ xiaogpt installed. Config: {result.config_path}",
            )

    # ── Hard Patch (open-xiaoai) ─────────────────────────────────────────────

    async def _install_hard_patch(
        self,
        choices: CustomizationChoices,
        *,
        result: AIBrainResult,
        on_output: Callable[[str, str], None] | None = None,
    ) -> None:
        """Install open-xiaoai hard patch.

        This approach:
        1. Completely replaces the Xiaomi voice engine
        2. Uses a Rust-based client for direct microphone access
        3. Supports custom local wake words (e.g., 'Hey Computer')
        4. Streams audio directly to an AI server endpoint
        5. Does NOT require Xiaomi cloud services

        Requires:
        - Pre-compiled ARM64 Rust binary (open-xiaoai-client)
        - AI server endpoint URL
        - Optional: custom wake word model
        """
        if on_output:
            on_output("stdout", "  Installing open-xiaoai (hard patch)...")

        # Step 1: Install Rust binary
        binary_name = "open-xiaoai-client"
        src_binary = self._binaries_dir / binary_name
        dst_binary = self._rootfs / "usr" / "bin" / binary_name
        dst_binary.parent.mkdir(parents=True, exist_ok=True)

        if src_binary.exists():
            shutil.copy2(src_binary, dst_binary)
            dst_binary.chmod(0o755 | stat.S_IEXEC)
            result.installed.append("open_xiaoai_binary")
            logger.info("Installed open-xiaoai binary")
        else:
            result.warnings.append(
                f"open-xiaoai binary not found: {src_binary}. "
                f"The AI brain will be non-functional. "
                f"Build from https://github.com/idootop/open-xiaoai for ARM64."
            )
            logger.warning("open-xiaoai binary not found: %s", src_binary)

        # Step 2: Install config
        open_xiaoai_dir = self._rootfs / "opt" / "open-xiaoai"
        open_xiaoai_dir.mkdir(parents=True, exist_ok=True)

        config = self._generate_open_xiaoai_config(choices)
        config_path = open_xiaoai_dir / "config.toml"
        config_path.write_text(config)
        result.config_path = str(config_path.relative_to(self._rootfs))
        result.installed.append("open_xiaoai_config")
        logger.info("Generated open-xiaoai config")

        # Step 3: Install wake word model if available
        wake_word_src = self._binaries_dir / "wake_word_model.bin"
        if wake_word_src.exists():
            wake_word_dst = open_xiaoai_dir / "wake_word_model.bin"
            shutil.copy2(wake_word_src, wake_word_dst)
            result.installed.append("wake_word_model")

        # Step 4: Install init script
        init_script = self._rootfs / "etc" / "init.d" / "S90open-xiaoai"
        init_script.parent.mkdir(parents=True, exist_ok=True)
        init_script.write_text(
            "#!/bin/sh\n"
            "# open-xiaoai AI brain (hard patch)\n"
            "# Installed by LX06 Flash Tool\n"
            "\n"
            "DAEMON=/usr/bin/open-xiaoai-client\n"
            "CONFIG=/opt/open-xiaoai/config.toml\n"
            "PIDFILE=/var/run/open-xiaoai.pid\n"
            "\n"
            "case \"$1\" in\n"
            "    start)\n"
            "        echo \"Starting open-xiaoai...\"\n"
            "        start-stop-daemon -S -b -m -p $PIDFILE \\\n"
            "            -x $DAEMON -- --config $CONFIG\n"
            "        ;;\n"
            "    stop)\n"
            "        echo \"Stopping open-xiaoai...\"\n"
            "        start-stop-daemon -K -p $PIDFILE\n"
            "        rm -f $PIDFILE\n"
            "        ;;\n"
            "    restart)\n"
            "        $0 stop\n"
            "        sleep 2\n"
            "        $0 start\n"
            "        ;;\n"
            "    *)\n"
            "        echo \"Usage: $0 {start|stop|restart}\"\n"
            "        exit 1\n"
            "        ;;\n"
            "esac\n"
        )
        init_script.chmod(0o755)
        result.installed.append("open_xiaoai_init")

        # Step 5: Disable stock Xiaomi services (hard patch replaces them)
        xiaoai_init = self._rootfs / "etc" / "init.d"
        if xiaoai_init.exists():
            for svc in xiaoai_init.iterdir():
                name = svc.name.lower()
                if "xiaoai" in name or "micclient" in name or "mibrain" in name:
                    # Rename to .disabled instead of deleting
                    disabled = svc.with_suffix(svc.suffix + ".disabled")
                    svc.rename(disabled)
                    result.installed.append(f"disabled_stock:{svc.name}")
                    logger.info("Disabled stock service: %s", svc.name)

        if on_output:
            on_output(
                "stdout",
                f"  ✅ open-xiaoai installed. Config: {result.config_path}",
            )

    # ── Config Generators ────────────────────────────────────────────────────

    def _generate_xiaogpt_config(self, choices: CustomizationChoices) -> str:
        """Generate xiao_config.yaml for xiaogpt.

        Maps user's API key selections to the xiaogpt configuration format.
        """
        llm_provider = choices.llm_provider or "openai"
        api_key = choices.llm_api_key or ""
        model = choices.llm_model or "gpt-4o-mini"
        api_base = choices.llm_api_base or ""

        config_lines = [
            "# xiaogpt configuration - generated by LX06 Flash Tool",
            "# Reference: https://github.com/yihong0618/xiaogpt",
            "",
            f"use_chatgpt_api: {'true' if llm_provider == 'openai' else 'false'}",
            f"api_key: \"{api_key}\"",
        ]

        if api_base:
            config_lines.append(f"api_base: \"{api_base}\"")

        config_lines.extend([
            f"model: \"{model}\"",
            "",
            "# Speaker settings",
            f"hardware: \"LX06\"",
            "mute_xiaoai: true",
            "stream: true",
            "",
            "# Network settings",
            "# The speaker must be on the same network as this device",
            "# xiaogpt will discover the speaker via SSDP",
        ])

        if llm_provider == "gemini":
            config_lines.extend([
                "",
                "# Gemini-specific settings",
                f"gemini_api_key: \"{api_key}\"",
            ])
        elif llm_provider == "kimi":
            config_lines.extend([
                "",
                "# Kimi-specific settings",
                f"kimi_api_key: \"{api_key}\"",
                "kimi_cookie: \"\"",
            ])

        return "\n".join(config_lines) + "\n"

    def _generate_open_xiaoai_config(self, choices: CustomizationChoices) -> str:
        """Generate config.toml for open-xiaoai.

        Configures the Rust client with AI server endpoint,
        wake word settings, and audio parameters.
        """
        server_url = choices.ai_server_url or "ws://localhost:8080/ws"
        wake_word = choices.custom_wake_word or "Hey Computer"
        sample_rate = 16000

        return (
            f"# open-xiaoai configuration - generated by LX06 Flash Tool\n"
            f"# Reference: https://github.com/idootop/open-xiaoai\n"
            f"\n"
            f"[audio]\n"
            f"sample_rate = {sample_rate}\n"
            f"channels = 1\n"
            f"frame_size = 512\n"
            f"\n"
            f"[wake_word]\n"
            f"keyword = \"{wake_word}\"\n"
            f"sensitivity = 0.5\n"
            f"model_path = \"/opt/open-xiaoai/wake_word_model.bin\"\n"
            f"\n"
            f"[server]\n"
            f"url = \"{server_url}\"\n"
            f"reconnect_interval_ms = 5000\n"
            f"timeout_ms = 30000\n"
            f"\n"
            f"[logging]\n"
            f"level = \"info\"\n"
            f"path = \"/var/log/open-xiaoai.log\"\n"
            f"\n"
            f"[tts]\n"
            f"engine = \"default\"\n"
            f"\n"
            f"[llm]\n"
            f"provider = \"{choices.llm_provider or 'openai'}\"\n"
            f"api_key = \"{choices.llm_api_key or ''}\"\n"
            f"model = \"{choices.llm_model or 'gpt-4o-mini'}\"\n"
        )
