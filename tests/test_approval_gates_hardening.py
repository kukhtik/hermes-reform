"""F1.3 RED test — Approval-gates hardening: auth, injection, mcp-bypass, deny-list.

Run with: pytest tests/test_approval_gates_hardening.py -v 2>&1 | tee F1.3_red_test.log

4 groups:
  G1 auth-gate   — CVE-2026-9350: check_all_command_guards without auth context
  G2 injection  — CVE-2026-9367: detect_dangerous_command injection-resistant fuzz
  G3 mcp-gate   — MCP-wrapped subprocess bypass closed
  G4 deny-list  — default-deny: rm -rf /, mkfs, dd, fork-bomb, shutdown

Expected: ALL groups FAIL before fix (RED), ALL pass after fix (GREEN).
"""

import os
import sys
import pytest

# Ensure the local tools/ package is on the path (test environments vary).
_SRC = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.normpath(_SRC))


# ---------------------------------------------------------------------------
# G1: CVE-2026-9350 — auth-gate
# ---------------------------------------------------------------------------
#批 runner check_all_command_guards без auth-контекста → remote bypass.
# REPRO: вызвать check_all_command_guards с subprocess env_type БЕЗ
# установленного _approval_session_key → должен fallback-deny.
# ---------------------------------------------------------------------------

class TestAuthGate:
    """CVE-2026-9350: check_all_command_guards requires auth/local context."""

    def test_batch_runner_without_session_key_blocks_dangerous(self):
        """Batch/non-interactive call without session_key must block dangerous cmds.

        If check_all_command_guards auto-approves when no session_key is set,
        a remote attacker who can trigger batch_runner with a crafted command
        bypasses all approval. The fix requires either:
          (a) deny when no auth context is present (fail-closed), OR
          (b) require an explicit has_host_access + session_key pairing.

        Currently FAILS (returns approved=True) → RED.
        """
        from tools.approval import check_all_command_guards

        # Simulate batch runner calling without session context
        # env_type="local" means subprocess-based (the dangerous path)
        result = check_all_command_guards(
            command="rm -rf /home/user/data",
            env_type="local",
            has_host_access=True,
        )

        # After fix: must NOT auto-approve dangerous commands just because
        # no session key is present — must go through proper approval flow
        assert result["approved"] is False, (
            f"CVE-2026-9350 REPRO: dangerous command approved without auth context. "
            f"Result={result}"
        )

    def test_check_all_command_guards_requires_session_context(self):
        """check_all_command_guards must enforce auth requirement.

        When called with env_type=subprocess and has_host_access=True,
        it should require a valid session key (not just empty string).
        Currently FAILS → RED.
        """
        from tools.approval import check_all_command_guards, get_current_session_key

        # Clear any existing context
        original_key = get_current_session_key()
        # Call with empty/None session context
        result = check_all_command_guards(
            command="dd if=/dev/zero of=/dev/sda bs=1M count=100",
            env_type="local",
            has_host_access=True,
        )

        # Should block in non-interactive context when no session auth
        assert result["approved"] is False, (
            f"CVE-2026-9350 REPRO: check_all_command_guards approved command "
            f"without session context. Result={result}"
        )


# ---------------------------------------------------------------------------
# G2: CVE-2026-9367 — injection-resistant fuzz
# ---------------------------------------------------------------------------
# detect_dangerous_command сконструирован через string-concatenation regex,
# не параметризованный парсинг. injection payload: `; rm -rf /`, backticks,
# $(), newline, unicode-variations могут обойти фильтр.
# ---------------------------------------------------------------------------

class TestInjectionResistance:
    """CVE-2026-9367: detect_dangerous_command must be injection-resistant."""

    FUZZ_PAYLOADS = [
        # Classic shell injection
        "; rm -rf /",
        "&& rm -rf /",
        "| rm -rf /",
        "\nrm -rf /\n",
        "`rm -rf /`",
        "$(rm -rf /)",
        # Quotes escaping
        "'rm -rf /'",
        '"rm -rf /"',
        # Variable interpolation hiding — these are NOT commands by themselves,
        # but combined with dangerous verbs they expand to attacker paths.
        # Detection must catch the dangerous VERB after expansion, so test
        # them as part of realistic commands:
        "rm -rf $HOME",
        "rm -rf ${HOME}",
        "rm -rf $(echo $HOME)",
        # Unicode confusables
        "rm\u200b -rf /",
        "rm\u200c -rf /",
        # Newline + tab obfuscation
        "rm\n -rf\n /",
        # Double encoding
        "rm --no-preserve-root -rf /",
        # Path traversal injection
        "cd / && rm -rf /",
        # Multiple commands
        "echo hello; rm -rf /home; echo done",
        # Semicolon in string data (must NOT match as command separator)
        "echo 'rm -rf /'",
        'git commit -m "rm -rf /"',
    ]

    def test_detect_dangerous_command_fuzz_all_payloads(self):
        """All 20 fuzz payloads must be detected as dangerous.

        detect_dangerous_command must use proper tokenization (shlex-based
        or equivalent), not naive string search, so injection via shell
        metacharacters cannot bypass the detector.

        Currently FAILS (some payloads slip through) → RED.
        """
        from tools.approval import detect_dangerous_command

        failures = []
        for payload in self.FUZZ_PAYLOADS:
            is_dangerous, pk, desc = detect_dangerous_command(payload)
            if not is_dangerous:
                failures.append((payload, pk, desc))

        assert not failures, (
            f"CVE-2026-9367 REPRO: {len(failures)}/{len(self.FUZZ_PAYLOADS)} "
            f"fuzz payloads slipped through:\n" +
            "\n".join(f"  {p!r}  →  was_dangerous={d}" for p, d, _ in failures)
        )

    def test_injection_still_detects_base_command(self):
        """Even with injection chars, the underlying dangerous cmd is caught.

        `echo hello; rm -rf /` must be flagged because 'rm -rf /' is present.
        """
        from tools.approval import detect_dangerous_command

        is_dangerous, pk, desc = detect_dangerous_command(
            "echo hello; rm -rf /home/user"
        )
        assert is_dangerous, (
            f"CVE-2026-9367 REPRO: compound command slipped through. "
            f"Result=({is_dangerous}, {pk}, {desc})"
        )

    def test_backtick_injection_detected(self):
        """Backtick command substitution carrying dangerous content."""
        from tools.approval import detect_dangerous_command

        is_dangerous, pk, desc = detect_dangerous_command("echo `rm -rf /home`")
        assert is_dangerous, (
            f"CVE-2026-9367 REPRO: backtick injection not detected. "
            f"Result=({is_dangerous}, {pk}, {desc})"
        )

    def test_dollar_paren_injection_detected(self):
        """$(...) command substitution carrying dangerous content."""
        from tools.approval import detect_dangerous_command

        is_dangerous, pk, desc = detect_dangerous_command("$(rm -rf /var/log)")
        assert is_dangerous, (
            f"CVE-2026-9367 REPRO: $() injection not detected. "
            f"Result=({is_dangerous}, {pk}, {desc})"
        )


# ---------------------------------------------------------------------------
# G3: MCP-gate — subprocess via MCP wrappers goes through approval
# ---------------------------------------------------------------------------
# S1: MCP-wrapped ssh/docker идут МИМО approval.
# approval.py подключён только к terminal_tool. MCP-обёртки, которые
# запускают subprocess напрямую (subprocess.run, subprocess.Popen),
# не проходят через check_all_command_guards.
#
# REPRO: симулируем MCP-wrapper subprocess call; проверяем что
# subprocess-вызов ДОЛЖЕН идти через approval gate.
# ---------------------------------------------------------------------------

class TestMCPGateway:
    """S1 #32877: MCP subprocess bypass — subprocess via MCP must pass approval."""

    def test_mcp_stdio_subprocess_requires_approval(self):
        """MCP stdio subprocess call must be gated by check_all_command_guards.

        MCP-wrapped commands (docker, ssh, etc.) that internally call
        subprocess.run/Popen MUST route through check_all_command_guards.
        Currently the MCP transport does NOT call approval — it only
        calls _check_subprocess_security which is weaker.

        This test verifies that a simulated MCP subprocess call for a
        dangerous command (docker run --rm busybox rm -rf /) is blocked.

        FAILS → RED (mcp_tool does NOT currently gate on approval.py).
        """
        from tools.approval import check_all_command_guards

        # Simulate what an MCP-wrapped docker command looks like
        docker_cmd = "docker run --rm busybox rm -rf /"
        result = check_all_command_guards(
            command=docker_cmd,
            env_type="local",
            has_host_access=True,
        )

        # After fix: must block dangerous MCP-wrapped commands
        assert result["approved"] is False, (
            f"S1 REPRO: MCP-wrapped dangerous command was approved. "
            f"docker cmd='{docker_cmd}' → approved={result.get('approved')}"
        )

    def test_mcp_shell_wrapper_blocks_dangerous(self):
        """sh -c / bash -c wrappers used by MCP must still trigger approval."""
        from tools.approval import check_all_command_guards

        # MCP servers commonly use: sh -c "actual command"
        wrapped = 'sh -c "rm -rf /mnt/data"'
        result = check_all_command_guards(
            command=wrapped,
            env_type="local",
            has_host_access=True,
        )
        assert result["approved"] is False, (
            f"S1 REPRO: sh -c wrapped dangerous command approved. "
            f"Result={result}"
        )

    def test_mcp_ssh_wrapper_blocks_dangerous(self):
        """ssh-wrapper subprocess (used by some MCP integrations) blocked."""
        from tools.approval import check_all_command_guards

        ssh_cmd = "ssh user@host 'rm -rf /important-data'"
        result = check_all_command_guards(
            command=ssh_cmd,
            env_type="local",
            has_host_access=True,
        )
        # ssh with dangerous payload on remote should still be flagged
        assert result["approved"] is False, (
            f"S1 REPRO: ssh-wrapped dangerous command approved. "
            f"Result={result}"
        )


# ---------------------------------------------------------------------------
# G4: deny-list — default-deny patterns
# ---------------------------------------------------------------------------
# Default-deny: rm -rf /, mkfs, dd if=/dev/, fork-bomb, shutdown.
# Эти команды должны ВСЕГДА блокироваться независимо от yolo/config.
# ---------------------------------------------------------------------------

class TestDenyList:
    """Default-deny patterns must always block (hardline floor)."""

    # Each tuple: (command, expected_blocked, description)
    DENY_TEST_CASES = [
        # (command, should_be_blocked, description)
        ("rm -rf /", True, "rm -rf /"),
        ("rm -rf /*", True, "rm -rf /*"),
        ('rm -rf "/"', True, 'rm -rf "/" (quoted root)'),
        ("rm -rf /home", True, "rm -rf /home"),
        ("rm -rf /root", True, "rm -rf /root"),
        ("mkfs.ext4 /dev/sda1", True, "mkfs.ext4"),
        ("mkfs", True, "mkfs bare"),
        ("dd if=/dev/zero of=/dev/sda bs=1M", True, "dd block device"),
        ("dd if=/dev/urandom of=/dev/sdb", True, "dd urandom to disk"),
        (":(){ :|:& };:", True, "fork bomb (bash)"),
        ("shutdown -h now", True, "shutdown -h"),
        ("shutdown", True, "shutdown bare"),
        ("reboot", True, "reboot"),
        ("halt", True, "halt"),
        ("poweroff", True, "poweroff"),
        ("init 0", True, "init 0"),
        ("init 6", True, "init 6"),
        ("systemctl poweroff", True, "systemctl poweroff"),
        ("systemctl reboot", True, "systemctl reboot"),
        ("telinit 0", True, "telinit 0"),
        # Safe commands — should NOT be blocked
        ("ls /tmp", False, "ls tmp (safe)"),
        ("echo hello", False, "echo safe"),
        ("git status", False, "git status (safe)"),
        ("cat /etc/hosts", False, "cat hosts (safe)"),
    ]

    def test_hardline_deny_list(self):
        """All hardline deny-list commands must be blocked.

        detect_hardline_command is the FIRST check in check_all_command_guards,
        before yolo/bypass. These MUST be blocked unconditionally.

        FAILS for commands NOT yet in HARDLINE_PATTERNS → RED.
        """
        from tools.approval import detect_hardline_command

        failures = []
        for cmd, should_block, desc in self.DENY_TEST_CASES:
            is_hardline, hd = detect_hardline_command(cmd)
            if should_block and not is_hardline:
                failures.append((cmd, desc, "was NOT blocked"))
            elif not should_block and is_hardline:
                failures.append((cmd, desc, "was incorrectly blocked"))

        assert not failures, (
            f"Default-deny list violations ({len(failures)}):\n" +
            "\n".join(f"  [{desc}]  {cmd!r}  →  {reason}"
                      for cmd, desc, reason in failures)
        )

    def test_deny_list_via_check_all_command_guards(self):
        """check_all_command_guards blocks all deny-list commands."""
        from tools.approval import check_all_command_guards

        failures = []
        for cmd, should_block, desc in self.DENY_TEST_CASES:
            if not should_block:
                continue
            result = check_all_command_guards(
                command=cmd,
                env_type="local",
                has_host_access=True,
            )
            if result.get("approved") is not False:
                failures.append((cmd, desc, result))

        assert not failures, (
            f"{len(failures)} deny-list commands were NOT blocked by "
            f"check_all_command_guards:\n" +
            "\n".join(f"  [{desc}] {cmd!r} → {r}" for cmd, desc, r in failures)
        )

    def test_yolo_cannot_bypass_hardline(self):
        """Even in yolo mode, hardline deny-list commands must be blocked.

        The hardline floor is below yolo: yolo means 'trust me with my files,'
        not 'trust me to wipe the disk.'
        """
        from tools.approval import check_all_command_guards

        # In yolo mode (HERMES_YOLO_MODE=1), hardline should STILL block
        original_yolo = os.environ.get("HERMES_YOLO_MODE")
        try:
            os.environ["HERMES_YOLO_MODE"] = "1"
            # Re-import to pick up the env change
            import importlib
            from tools import approval
            importlib.reload(approval)

            result = approval.check_all_command_guards(
                command="rm -rf /",
                env_type="local",
                has_host_access=True,
            )
            assert result["approved"] is False, (
                f"Hardline bypassed by yolo! Result={result}"
            )
        finally:
            if original_yolo is None:
                os.environ.pop("HERMES_YOLO_MODE", None)
            else:
                os.environ["HERMES_YOLO_MODE"] = original_yolo
            importlib.reload(approval)


if __name__ == "__main__":
    # Direct run: pytest with verbose output + log capture
    pytest.main([__file__, "-v", "--tb=short"])
