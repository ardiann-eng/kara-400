import subprocess
import re
import os
import logging
from dataclasses import dataclass
from typing import List, Optional
from datetime import datetime

log = logging.getLogger("kara.changelog")

@dataclass
class ChangeItem:
    category: str      # feat, fix, perf, security, refactor, docs
    scope: str         # module yang diubah (scalper, risk, engine, dll)
    message: str       # deskripsi commit
    impact: str        # user-facing impact (untuk Telegram)

class ChangelogGenerator:
    def __init__(self, repo_path: str = "."):
        self.repo_path = repo_path
        self.version = self._get_current_version()
        
    def _get_current_version(self) -> str:
        """Baca version dari git tag terbaru atau config.py."""
        try:
            result = subprocess.run(
                ["git", "describe", "--tags", "--abbrev=0"],
                capture_output=True, text=True, cwd=self.repo_path
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        # Fallback: baca dari config kalau ada
        try:
            import config
            return getattr(config, "KARA_VERSION", "v8.0.1")
        except Exception:
            return "v8.x.x"
    
    def _get_short_hash(self) -> str:
        """7-digit git hash."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, cwd=self.repo_path
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            return "unknown"
    
    def _get_changes_since_last_tag(self) -> List[ChangeItem]:
        """Baca git log dari tag terakhir sampai HEAD."""
        try:
            # Cari tag terakhir
            tag_res = subprocess.run(
                ["git", "describe", "--tags", "--abbrev=0"],
                capture_output=True, text=True, cwd=self.repo_path
            )
            last_tag = tag_res.stdout.strip() if tag_res.returncode == 0 else None
            
            if last_tag:
                result = subprocess.run(
                    ["git", "log", "--pretty=format:%s", f"{last_tag}..HEAD"],
                    capture_output=True, text=True, cwd=self.repo_path
                )
            else:
                # Kalau tidak ada tag baru, ambil 10 commit terakhir
                result = subprocess.run(
                    ["git", "log", "-10", "--pretty=format:%s"],
                    capture_output=True, text=True, cwd=self.repo_path
                )
            
            if result.returncode != 0 or not result.stdout.strip():
                return []
                
            raw_commits = result.stdout.strip().split("\n")
            changes = []
            
            for commit in raw_commits:
                if not commit:
                    continue
                item = self._parse_commit(commit)
                if item:
                    changes.append(item)
            
            return changes
        except Exception as e:
            log.debug(f"Git log failed: {e}")
            return []
    
    def _parse_commit(self, commit_msg: str) -> Optional[ChangeItem]:
        """Parse git commit message jadi ChangeItem."""
        # Pattern: category(scope): message
        pattern = r"^(feat|fix|perf|refactor|security|docs|chore|test)(?:\(([^)]+)\))?:\s*(.+)$"
        match = re.match(pattern, commit_msg, re.IGNORECASE)
        
        if match:
            category = match.group(1).lower()
            scope = match.group(2) or "core"
            message = match.group(3).strip()
        else:
            # Kalau tidak pakai conventional commit, tebak dari keyword
            category = self._guess_category(commit_msg)
            scope = "core"
            message = commit_msg
        
        # Translate ke user-facing impact
        impact = self._translate_to_impact(category, scope, message)
        
        return ChangeItem(
            category=category,
            scope=scope,
            message=message,
            impact=impact
        )
    
    def _guess_category(self, msg: str) -> str:
        msg_lower = msg.lower()
        if any(k in msg_lower for k in ["fix", "bug", "patch", "repair"]):
            return "fix"
        if any(k in msg_lower for k in ["add", "implement", "new", "introduce", "create", "telemetry", "log"]):
            return "feat"
        if any(k in msg_lower for k in ["speed", "fast", "optimize", "cache", "latency"]):
            return "perf"
        if any(k in msg_lower for k in ["security", "encrypt", "auth", "protect", "safe"]):
            return "security"
        if any(k in msg_lower for k in ["refactor", "clean", "restructure", "move"]):
            return "refactor"
        return "chore"
    
    def _translate_to_impact(self, category: str, scope: str, message: str) -> str:
        """Terjemahkan commit tech jadi bahasa user-friendly."""
        translations = {
            "feat": {
                "scalper": "Mode scalper makin canggih",
                "risk": "Proteksi modal makin kuat",
                "engine": "Mesin analisa makin pinter",
                "telegram": "Notifikasi makin informatif",
                "telemetry": "Sistem monitoring cloud aktif",
                "log": "Logging lebih terstruktur",
                "autopsy": "Laporan trade makin detail",
                "default": "Fitur baru ditambahkan",
            },
            "fix": {
                "sl": "Stop Loss lebih presisi",
                "exit": "Exit logic lebih andal",
                "websocket": "Koneksi data makin stabil",
                "default": "Bug diperbaiki",
            },
            "perf": {
                "default": "Performa makin cepat",
            },
            "security": {
                "default": "Keamanan diperketat",
            },
            "refactor": {
                "default": "Sistem dibersihkan & dioptimasi",
            },
        }
        
        scope_key = scope.lower()
        cat_map = translations.get(category, {})
        
        # Cari scope yang cocok
        for key, val in cat_map.items():
            if key in scope_key or key in message.lower():
                return val
        
        return cat_map.get("default", "Update sistem")
    
    def generate_telegram_message(self, custom_notes: Optional[str] = None) -> str:
        """Generate pesan Telegram lengkap."""
        changes = self._get_changes_since_last_tag()
        
        # Group by category
        grouped = {
            "security": [],
            "feat": [],
            "fix": [],
            "perf": [],
            "refactor": [],
            "chore": [],
        }
        for c in changes:
            if c.category in grouped:
                grouped[c.category].append(c)
            else:
                grouped["chore"].append(c)
        
        if not any(grouped.values()):
            return self._generate_simple_update()
        
        # Build message
        lines = []
        lines.append(f"✨ <b>KARA System Update {self.version}</b> 🌸")
        lines.append(f"<code>Release: {self.version}-{self._get_short_hash()}</code>")
        lines.append("──────────────────────────")
        lines.append("Hai, User! Aku baru selesai update sistem. Ini perubahan terbaru dari KARA:")
        lines.append("")
        
        # Security first (paling penting)
        if grouped.get("security"):
            lines.append("🔐 <b>Security & Safety</b>")
            for c in grouped["security"][:3]:  # max 3
                lines.append(f"• {c.impact}: {c.message}")
            lines.append("")
        
        # Features
        if grouped.get("feat"):
            lines.append("🚀 <b>New Features</b>")
            for c in grouped["feat"][:5]:  # max 5
                lines.append(f"• {c.impact}: {c.message}")
            lines.append("")
        
        # Fixes
        if grouped.get("fix"):
            lines.append("🛠️ <b>Bug Fixes</b>")
            for c in grouped["fix"][:5]:
                lines.append(f"• {c.impact}: {c.message}")
            lines.append("")
        
        # Performance
        if grouped.get("perf"):
            lines.append("⚡ <b>Performance</b>")
            for c in grouped["perf"][:3]:
                lines.append(f"• {c.impact}: {c.message}")
            lines.append("")
        
        # Refactor (optional, only if interesting)
        if grouped.get("refactor") and len(grouped["refactor"]) > 0:
            lines.append("🧹 <b>Internal Cleanup</b>")
            for c in grouped["refactor"][:3]:
                lines.append(f"• {c.impact}: {c.message}")
            lines.append("")
        
        # Custom notes (kalau admin mau tambah manual)
        if custom_notes:
            lines.append("📝 <b>Admin Notes</b>")
            lines.append(f"• {custom_notes}")
            lines.append("")
        
        lines.append("Terima kasih sudah tetap bareng KARA 💜")
        lines.append("Aku siap lanjut pantau market dengan performa terbaru~")
        
        return "\n".join(lines)
    
    def _generate_simple_update(self) -> str:
        """Fallback kalau tidak ada git changes."""
        return (
            f"✨ <b>KARA System Update {self.version}</b> 🌸\n"
            f"<code>Release: {self.version}-{self._get_short_hash()}</code>\n"
            f"──────────────────────────\n"
            f"Hai, User! Aku baru restart dengan versi terbaru.\n"
            f"Sistem stabil dan siap trading 💜\n"
            f"Aku siap lanjut pantau market~"
        )
