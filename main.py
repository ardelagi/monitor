import discord
from discord.ext import tasks
from discord.ui import Button, View, Select
import psutil
import platform
import asyncio
import datetime
from typing import Optional, Dict, List
import json
import os
import time
import subprocess
import socket
from collections import deque
import math

# ===== KONFIGURASI =====
CONFIG = {
    "token": "YOUR_BOT_TOKEN",
    "channel_id": 0,
    "update_interval": 30,
    "embed_color": 0x00ff00,
    "admin_role_ids": [],  # IDs role yang bisa jalankan command admin
    "admin_user_ids": [],  # IDs user yang bisa jalankan command admin
    "alert_channel_id": 0,  # Channel untuk alert
    "thresholds": {
        "cpu": 80,
        "memory": 85,
        "disk": 90,
        "temperature": 75
    },
    "view_mode": "detailed",  # detailed, compact
    "color_mode": "dynamic",  # dynamic, static
    "enable_alerts": True,
    "monitor_docker": False,
    "monitor_services": [],  # List service yang mau dimonitor
}

class DataStore:
    """Manajemen data dengan JSON"""
    def __init__(self, filename='monitor_data.json'):
        self.filename = filename
        self.data = self.load()
    
    def load(self) -> dict:
        """Load data dari file"""
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r') as f:
                    return json.load(f)
            except:
                pass
        return {
            "history": [],
            "alerts": [],
            "audit_logs": [],
            "stats_summary": {}
        }
    
    def save(self):
        """Save data ke file"""
        try:
            with open(self.filename, 'w') as f:
                json.dump(self.data, f, indent=4)
        except Exception as e:
            print(f"Error saving data: {e}")
    
    def add_history(self, stats: dict):
        """Tambah history entry (max 1000)"""
        self.data["history"].append({
            "timestamp": datetime.datetime.now().isoformat(),
            "stats": stats
        })
        # Keep only last 1000 entries
        if len(self.data["history"]) > 1000:
            self.data["history"] = self.data["history"][-1000:]
        self.save()
    
    def add_alert(self, alert_type: str, message: str, value: float):
        """Tambah alert log"""
        self.data["alerts"].append({
            "timestamp": datetime.datetime.now().isoformat(),
            "type": alert_type,
            "message": message,
            "value": value
        })
        if len(self.data["alerts"]) > 500:
            self.data["alerts"] = self.data["alerts"][-500:]
        self.save()
    
    def add_audit_log(self, user: str, command: str, success: bool):
        """Tambah audit log"""
        self.data["audit_logs"].append({
            "timestamp": datetime.datetime.now().isoformat(),
            "user": user,
            "command": command,
            "success": success
        })
        if len(self.data["audit_logs"]) > 500:
            self.data["audit_logs"] = self.data["audit_logs"][-500:]
        self.save()
    
    def get_history(self, hours: int = 24) -> list:
        """Get history untuk X jam terakhir"""
        cutoff = datetime.datetime.now() - datetime.timedelta(hours=hours)
        return [
            entry for entry in self.data["history"]
            if datetime.datetime.fromisoformat(entry["timestamp"]) > cutoff
        ]

class NetworkMonitor:
    def __init__(self):
        self.last_bytes_sent = 0
        self.last_bytes_recv = 0
        self.last_check_time = time.time()
        self.current_sent_rate = 0
        self.current_recv_rate = 0
        self.peak_sent_rate = 0
        self.peak_recv_rate = 0
        
    def update_rates(self, bytes_sent, bytes_recv):
        current_time = time.time()
        time_diff = current_time - self.last_check_time
        
        if self.last_bytes_sent > 0 and time_diff > 0:
            sent_diff = bytes_sent - self.last_bytes_sent
            recv_diff = bytes_recv - self.last_bytes_recv
            
            self.current_sent_rate = sent_diff / time_diff / 1024  # KB/s
            self.current_recv_rate = recv_diff / time_diff / 1024  # KB/s
            
            # Update peaks
            self.peak_sent_rate = max(self.peak_sent_rate, self.current_sent_rate)
            self.peak_recv_rate = max(self.peak_recv_rate, self.current_recv_rate)
        
        self.last_bytes_sent = bytes_sent
        self.last_bytes_recv = bytes_recv
        self.last_check_time = current_time

class StatsView(View):
    """Interactive buttons untuk stats"""
    def __init__(self, monitor):
        super().__init__(timeout=None)
        self.monitor = monitor
    
    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.primary, custom_id="refresh")
    async def refresh_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer()
        await self.monitor.send_or_update_stats()
    
    @discord.ui.button(label="📊 History", style=discord.ButtonStyle.secondary, custom_id="history")
    async def history_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer()
        await self.monitor.send_history_stats(interaction)
    
    @discord.ui.button(label="🔔 Alerts", style=discord.ButtonStyle.secondary, custom_id="alerts")
    async def alerts_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer()
        await self.monitor.send_alert_summary(interaction)
    
    @discord.ui.button(label="⚙️ Config", style=discord.ButtonStyle.secondary, custom_id="config")
    async def config_button(self, interaction: discord.Interaction, button: Button):
        if not self.monitor.is_admin(interaction.user):
            await interaction.response.send_message("❌ Admin only!", ephemeral=True)
            return
        await interaction.response.defer()
        await self.monitor.send_config_info(interaction)

class ServerMonitor:
    def __init__(self):
        # Setup Discord client
        intents = discord.Intents.default()
        intents.message_content = True
        self.client = discord.Client(intents=intents)
        
        # Variables
        self.status_message: Optional[discord.Message] = None
        self.start_time = datetime.datetime.now()
        self.network_monitor = NetworkMonitor()
        self.data_store = DataStore()
        self.last_alert_time = {}
        self.alert_cooldown = 300  # 5 minutes cooldown per alert type
        
        # Setup events
        self.setup_events()
    
    def is_admin(self, user) -> bool:
        """Check if user is admin"""
        if user.id in CONFIG["admin_user_ids"]:
            return True
        if hasattr(user, 'roles'):
            for role in user.roles:
                if role.id in CONFIG["admin_role_ids"]:
                    return True
        return False
    
    def setup_events(self):
        @self.client.event
        async def on_ready():
            print(f'Bot logged in as {self.client.user}!')
            print(f'Starting auto-update every {CONFIG["update_interval"]} seconds...')
            
            # Start the monitoring loop
            self.update_stats.start()
            self.check_alerts.start()
        
        @self.client.event
        async def on_message(message):
            if message.author.bot:
                return
            
            cmd = message.content.lower().strip()
            
            # Public commands
            if cmd == '!updatestats' or cmd == '!stats':
                await self.send_or_update_stats()
                await message.add_reaction('✅')
            
            elif cmd == '!setstats':
                self.status_message = None
                await self.send_or_update_stats()
                await message.add_reaction('🔄')
            
            elif cmd.startswith('!history'):
                parts = cmd.split()
                hours = 24
                if len(parts) > 1:
                    try:
                        hours = int(parts[1].replace('h', ''))
                    except:
                        pass
                await self.send_history_stats(message, hours)
            
            elif cmd == '!alerts':
                await self.send_alert_summary(message)
            
            elif cmd == '!help':
                await self.send_help(message)
            
            # Admin commands
            elif cmd.startswith('!config'):
                if not self.is_admin(message.author):
                    await message.reply("❌ Admin only!")
                    return
                await self.handle_config_command(message)
            
            elif cmd == '!audit':
                if not self.is_admin(message.author):
                    await message.reply("❌ Admin only!")
                    return
                await self.send_audit_logs(message)
            
            elif cmd.startswith('!service'):
                if not self.is_admin(message.author):
                    await message.reply("❌ Admin only!")
                    return
                await self.handle_service_command(message)
    
    def get_progress_bar(self, percentage: float, length: int = 10) -> str:
        """Generate progress bar dengan emoji"""
        filled = int((percentage / 100) * length)
        bar = "▓" * filled + "░" * (length - filled)
        return f"{bar} {percentage:.1f}%"
    
    def get_temperature(self) -> dict:
        """Get temperature info"""
        try:
            temps = psutil.sensors_temperatures()
            if temps:
                # Get first available temperature sensor
                for name, entries in temps.items():
                    if entries:
                        return {
                            "current": entries[0].current,
                            "high": entries[0].high if entries[0].high else 100,
                            "critical": entries[0].critical if entries[0].critical else 100
                        }
        except:
            pass
        return {"current": 0, "high": 0, "critical": 0}
    
    def get_docker_stats(self) -> list:
        """Get Docker container stats"""
        if not CONFIG["monitor_docker"]:
            return []
        
        try:
            result = subprocess.run(
                ['docker', 'ps', '--format', '{{.Names}}|{{.Status}}|{{.ID}}'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                containers = []
                for line in result.stdout.strip().split('\n'):
                    if line:
                        parts = line.split('|')
                        if len(parts) >= 3:
                            containers.append({
                                "name": parts[0],
                                "status": parts[1],
                                "id": parts[2]
                            })
                return containers
        except:
            pass
        return []
    
    def get_service_status(self, service_name: str) -> dict:
        """Check service status"""
        try:
            result = subprocess.run(
                ['systemctl', 'is-active', service_name],
                capture_output=True,
                text=True,
                timeout=5
            )
            is_active = result.stdout.strip() == 'active'
            return {
                "name": service_name,
                "status": "running" if is_active else "stopped",
                "active": is_active
            }
        except:
            return {
                "name": service_name,
                "status": "unknown",
                "active": False
            }
    
    def get_top_processes(self, count: int = 5) -> list:
        """Get top processes by CPU usage"""
        try:
            processes = []
            for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']):
                try:
                    info = proc.info
                    processes.append({
                        'name': info['name'],
                        'cpu': info['cpu_percent'],
                        'memory': info['memory_percent']
                    })
                except:
                    continue
            
            # Sort by CPU usage
            processes.sort(key=lambda x: x['cpu'], reverse=True)
            return processes[:count]
        except:
            return []
    
    def get_cpu_info(self) -> dict:
        """Mendapatkan informasi CPU dengan temperature"""
        try:
            cpu_model = "Unknown Processor"
            try:
                with open('/proc/cpuinfo', 'r') as f:
                    lines = f.readlines()
                    for line in lines:
                        if 'model name' in line:
                            cpu_model = line.split(':')[1].strip()
                            break
            except:
                cpu_model = platform.processor() or f"{platform.machine()} Processor"
            
            cpu_freq = psutil.cpu_freq()
            cpu_count_physical = psutil.cpu_count(logical=False)
            cpu_count_logical = psutil.cpu_count(logical=True)
            cpu_usage = psutil.cpu_percent(interval=1)
            cpu_per_core = psutil.cpu_percent(interval=1, percpu=True)
            
            temp_info = self.get_temperature()
            
            return {
                "model": cpu_model,
                "usage": cpu_usage,
                "per_core": cpu_per_core,
                "cores_physical": cpu_count_physical or cpu_count_logical // 2,
                "cores_logical": cpu_count_logical,
                "frequency": cpu_freq.current if cpu_freq else "N/A",
                "temperature": temp_info["current"]
            }
        except Exception as e:
            print(f"Error getting CPU info: {e}")
            return {
                "model": "Unknown Processor",
                "usage": 0,
                "per_core": [],
                "cores_physical": 1,
                "cores_logical": 2,
                "frequency": "N/A",
                "temperature": 0
            }
    
    def get_memory_info(self) -> dict:
        """Mendapatkan informasi Memory"""
        try:
            memory = psutil.virtual_memory()
            swap = psutil.swap_memory()
            
            return {
                "total": memory.total / (1024**3),
                "used": memory.used / (1024**3),
                "available": memory.available / (1024**3),
                "percentage": memory.percent,
                "swap_total": swap.total / (1024**3),
                "swap_used": swap.used / (1024**3),
                "swap_percentage": swap.percent
            }
        except Exception as e:
            print(f"Error getting memory info: {e}")
            return {
                "total": 0,
                "used": 0,
                "available": 0,
                "percentage": 0,
                "swap_total": 0,
                "swap_used": 0,
                "swap_percentage": 0
            }
    
    def get_disk_info(self) -> dict:
        """Mendapatkan informasi Disk"""
        try:
            disk = psutil.disk_usage('/')
            io_counters = psutil.disk_io_counters()
            
            total_gb = disk.total / (1024**3)
            used_gb = disk.used / (1024**3)
            
            if total_gb > 1000:
                total_display = f"{total_gb / 1024:.2f} TB"
                used_display = f"{used_gb:.2f} GB"
            else:
                total_display = f"{total_gb:.2f} GB"
                used_display = f"{used_gb:.2f} GB"
            
            return {
                "total": total_gb,
                "used": used_gb,
                "free": disk.free / (1024**3),
                "percentage": (disk.used / disk.total) * 100,
                "total_display": total_display,
                "used_display": used_display,
                "read_bytes": io_counters.read_bytes if io_counters else 0,
                "write_bytes": io_counters.write_bytes if io_counters else 0
            }
        except:
            try:
                disk = psutil.disk_usage('C:\\')
                total_gb = disk.total / (1024**3)
                used_gb = disk.used / (1024**3)
                
                if total_gb > 1000:
                    total_display = f"{total_gb / 1024:.2f} TB"
                    used_display = f"{used_gb:.2f} GB"
                else:
                    total_display = f"{total_gb:.2f} GB"
                    used_display = f"{used_gb:.2f} GB"
                
                return {
                    "total": total_gb,
                    "used": used_gb,
                    "free": disk.free / (1024**3),
                    "percentage": (disk.used / disk.total) * 100,
                    "total_display": total_display,
                    "used_display": used_display,
                    "read_bytes": 0,
                    "write_bytes": 0
                }
            except Exception as e:
                print(f"Error getting disk info: {e}")
                return {
                    "total": 0,
                    "used": 0,
                    "free": 0,
                    "percentage": 0,
                    "total_display": "0 GB",
                    "used_display": "0 GB",
                    "read_bytes": 0,
                    "write_bytes": 0
                }
    
    def get_network_info(self) -> dict:
        """Mendapatkan informasi Network"""
        try:
            net_io = psutil.net_io_counters()
            connections = len(psutil.net_connections())
            
            self.network_monitor.update_rates(net_io.bytes_sent, net_io.bytes_recv)
            
            total_sent = self.format_bytes_network(net_io.bytes_sent)
            total_recv = self.format_bytes_network(net_io.bytes_recv)
            
            return {
                "bytes_sent": net_io.bytes_sent,
                "bytes_recv": net_io.bytes_recv,
                "current_sent": f"{self.network_monitor.current_sent_rate:.2f} KB/s",
                "current_recv": f"{self.network_monitor.current_recv_rate:.2f} KB/s",
                "peak_sent": f"{self.network_monitor.peak_sent_rate:.2f} KB/s",
                "peak_recv": f"{self.network_monitor.peak_recv_rate:.2f} KB/s",
                "total_sent": total_sent,
                "total_recv": total_recv,
                "connections": connections
            }
        except Exception as e:
            print(f"Error getting network info: {e}")
            return {
                "bytes_sent": 0,
                "bytes_recv": 0,
                "current_sent": "0.00 KB/s",
                "current_recv": "0.00 KB/s",
                "peak_sent": "0.00 KB/s",
                "peak_recv": "0.00 KB/s",
                "total_sent": "0 B",
                "total_recv": "0 B",
                "connections": 0
            }
    
    def format_bytes_network(self, bytes_value: int) -> str:
        """Format bytes untuk network stats"""
        if bytes_value >= 1024**4:
            return f"{bytes_value / (1024**4):.2f} TB"
        elif bytes_value >= 1024**3:
            return f"{bytes_value / (1024**3):.2f} GB"
        elif bytes_value >= 1024**2:
            return f"{bytes_value / (1024**2):.2f} MB"
        elif bytes_value >= 1024:
            return f"{bytes_value / 1024:.2f} KB"
        else:
            return f"{bytes_value} B"
    
    def get_uptime(self) -> str:
        """Mendapatkan system uptime"""
        try:
            boot_time = datetime.datetime.fromtimestamp(psutil.boot_time())
            uptime = datetime.datetime.now() - boot_time
            
            days = uptime.days
            hours, remainder = divmod(uptime.seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            
            return f"{days}d {hours}h {minutes}m {seconds}s"
        except Exception as e:
            print(f"Error getting uptime: {e}")
            return "N/A"
    
    async def get_discord_ping(self) -> int:
        """Mendapatkan ping ke Discord"""
        return round(self.client.latency * 1000)
    
    def get_dynamic_color(self, cpu: float, memory: float, disk: float) -> int:
        """Get dynamic color based on usage"""
        if CONFIG["color_mode"] != "dynamic":
            return CONFIG["embed_color"]
        
        max_usage = max(cpu, memory, disk)
        
        if max_usage >= 90:
            return 0xff0000  # Red
        elif max_usage >= 75:
            return 0xff6600  # Orange
        elif max_usage >= 50:
            return 0xffff00  # Yellow
        else:
            return 0x00ff00  # Green
    
    async def create_stats_embed(self) -> discord.Embed:
        """Membuat embed dengan statistik server yang lebih lengkap"""
        cpu_info = self.get_cpu_info()
        memory_info = self.get_memory_info()
        disk_info = self.get_disk_info()
        network_info = self.get_network_info()
        uptime = self.get_uptime()
        discord_ping = await self.get_discord_ping()
        top_processes = self.get_top_processes(3)
        
        # Dynamic color
        color = self.get_dynamic_color(
            cpu_info['usage'],
            memory_info['percentage'],
            disk_info['percentage']
        )
        
        embed = discord.Embed(
            title="📊 Server Stats - Enhanced Monitor",
            color=color
        )
        
        # View mode
        if CONFIG["view_mode"] == "compact":
            description = self._create_compact_view(
                cpu_info, memory_info, disk_info, network_info, uptime
            )
        else:
            description = self._create_detailed_view(
                cpu_info, memory_info, disk_info, network_info, uptime, top_processes
            )
        
        embed.description = description
        
        # Additional fields
        if CONFIG["monitor_docker"]:
            containers = self.get_docker_stats()
            if containers:
                container_text = "\n".join([
                    f"• {c['name']}: {c['status']}" for c in containers[:5]
                ])
                embed.add_field(
                    name="🐳 Docker Containers",
                    value=container_text,
                    inline=False
                )
        
        if CONFIG["monitor_services"]:
            services_text = ""
            for service in CONFIG["monitor_services"][:5]:
                status = self.get_service_status(service)
                emoji = "✅" if status["active"] else "❌"
                services_text += f"{emoji} {service}: {status['status']}\n"
            if services_text:
                embed.add_field(
                    name="⚙️ Services",
                    value=services_text,
                    inline=False
                )
        
        # Discord ping
        embed.add_field(
            name="🌐 Discord API Ping",
            value=f"{discord_ping} ms",
            inline=False
        )
        
        # Footer
        now = datetime.datetime.now()
        embed.set_footer(
            text=f"🔄 Auto-updates every {CONFIG['update_interval']}s | Last: {now.strftime('%H:%M:%S')}"
        )
        
        return embed
    
    def _create_detailed_view(self, cpu, mem, disk, net, uptime, processes) -> str:
        """Create detailed view"""
        view = "**💻 System Information**\n\n"
        
        # CPU
        view += f"**CPU:** {cpu['model'][:50]}\n"
        view += f"{self.get_progress_bar(cpu['usage'])}\n"
        if cpu['temperature'] > 0:
            view += f"🌡️ Temp: {cpu['temperature']:.1f}°C\n"
        view += f"Cores: {cpu['cores_physical']}P/{cpu['cores_logical']}L"
        if cpu['frequency'] != "N/A":
            view += f" @ {cpu['frequency']:.0f} MHz"
        view += "\n\n"
        
        # Memory
        view += f"**💾 Memory**\n"
        view += f"{self.get_progress_bar(mem['percentage'])}\n"
        view += f"Used: {mem['used']:.2f} GB / {mem['total']:.2f} GB\n"
        if mem['swap_total'] > 0:
            view += f"Swap: {mem['swap_used']:.2f} GB / {mem['swap_total']:.2f} GB\n"
        view += "\n"
        
        # Disk
        view += f"**💿 Disk**\n"
        view += f"{self.get_progress_bar(disk['percentage'])}\n"
        view += f"Used: {disk['used_display']} / {disk['total_display']}\n\n"
        
        # Network
        view += f"**🌐 Network**\n"
        view += f"↑ {net['current_sent']} (Peak: {net['peak_sent']})\n"
        view += f"↓ {net['current_recv']} (Peak: {net['peak_recv']})\n"
        view += f"Total: ↑{net['total_sent']} ↓{net['total_recv']}\n"
        view += f"Active Connections: {net['connections']}\n\n"
        
        # Top Processes
        if processes:
            view += f"**⚡ Top Processes**\n"
            for proc in processes:
                view += f"• {proc['name'][:20]}: {proc['cpu']:.1f}% CPU\n"
            view += "\n"
        
        # Uptime
        view += f"**⏰ Uptime:** {uptime}"
        
        return view
    
    def _create_compact_view(self, cpu, mem, disk, net, uptime) -> str:
        """Create compact view"""
        view = f"**CPU:** {cpu['usage']:.1f}% | "
        view += f"**RAM:** {mem['percentage']:.1f}% | "
        view += f"**Disk:** {disk['percentage']:.1f}%\n"
        view += f"**Network:** ↑{net['current_sent']} ↓{net['current_recv']}\n"
        view += f"**Uptime:** {uptime}"
        return view
    
    async def check_threshold_alerts(self):
        """Check if any thresholds are exceeded"""
        if not CONFIG["enable_alerts"]:
            return
        
        cpu_info = self.get_cpu_info()
        memory_info = self.get_memory_info()
        disk_info = self.get_disk_info()
        
        alerts = []
        
        # Check CPU
        if cpu_info['usage'] > CONFIG['thresholds']['cpu']:
            if self._can_send_alert('cpu'):
                alerts.append(('cpu', f"CPU usage is high: {cpu_info['usage']:.1f}%", cpu_info['usage']))
        
        # Check Memory
        if memory_info['percentage'] > CONFIG['thresholds']['memory']:
            if self._can_send_alert('memory'):
                alerts.append(('memory', f"Memory usage is high: {memory_info['percentage']:.1f}%", memory_info['percentage']))
        
        # Check Disk
        if disk_info['percentage'] > CONFIG['thresholds']['disk']:
            if self._can_send_alert('disk'):
                alerts.append(('disk', f"Disk usage is high: {disk_info['percentage']:.1f}%", disk_info['percentage']))
        
        # Check Temperature
        if cpu_info['temperature'] > CONFIG['thresholds']['temperature']:
            if self._can_send_alert('temperature'):
                alerts.append(('temperature', f"CPU temperature is high: {cpu_info['temperature']:.1f}°C", cpu_info['temperature']))
        
        # Send alerts
        for alert_type, message, value in alerts:
            await self.send_alert(alert_type, message, value)
    
    def _can_send_alert(self, alert_type: str) -> bool:
        """Check if we can send alert (cooldown)"""
        now = time.time()
        if alert_type in self.last_alert_time:
            if now - self.last_alert_time[alert_type] < self.alert_cooldown:
                return False
        self.last_alert_time[alert_type] = now
        return True
    
    async def send_alert(self, alert_type: str, message: str, value: float):
        """Send alert to alert channel"""
        self.data_store.add_alert(alert_type, message, value)
        
        if CONFIG["alert_channel_id"] == 0:
            return
        
        try:
            channel = self.client.get_channel(CONFIG["alert_channel_id"])
            if channel:
                embed = discord.Embed(
                    title="⚠️ Alert Triggered",
                    description=message,
                    color=0xff6600,
                    timestamp=datetime.datetime.now()
                )
                embed.add_field(name="Type", value=alert_type.upper())
                embed.add_field(name="Value", value=f"{value:.2f}")
                await channel.send(embed=embed)
        except Exception as e:
            print(f"Error sending alert: {e}")
    
    async def send_history_stats(self, ctx, hours: int = 24):
        """Send historical stats"""
        history = self.data_store.get_history(hours)
        
        if not history:
            embed = discord.Embed(
                title="📊 Historical Stats",
                description="No historical data available yet.",
                color=0xff6600
            )
            if hasattr(ctx, 'channel'):
                await ctx.channel.send(embed=embed)
            else:
                await ctx.followup.send(embed=embed, ephemeral=True)
            return
        
        # Calculate averages
        cpu_avg = sum(h['stats']['cpu'] for h in history if 'cpu' in h['stats']) / len(history)
        mem_avg = sum(h['stats']['memory'] for h in history if 'memory' in h['stats']) / len(history)
        disk_avg = sum(h['stats']['disk'] for h in history if 'disk' in h['stats']) / len(history)
        
        # Find peaks
        cpu_peak = max(h['stats']['cpu'] for h in history if 'cpu' in h['stats'])
        mem_peak = max(h['stats']['memory'] for h in history if 'memory' in h['stats'])
        
        embed = discord.Embed(
            title=f"📊 Historical Stats (Last {hours}h)",
            color=0x00ff00
        )
        
        embed.add_field(
            name="CPU",
            value=f"Avg: {cpu_avg:.1f}%\nPeak: {cpu_peak:.1f}%",
            inline=True
        )
        
        embed.add_field(
            name="Memory",
            value=f"Avg: {mem_avg:.1f}%\nPeak: {mem_peak:.1f}%",
            inline=True
        )
        
        embed.add_field(
            name="Disk",
            value=f"Avg: {disk_avg:.1f}%",
            inline=True
        )
        
        embed.add_field(
            name="Data Points",
            value=f"{len(history)} samples",
            inline=False
        )
        
        embed.set_footer(text=f"Monitoring since {datetime.datetime.fromisoformat(history[0]['timestamp']).strftime('%Y-%m-%d %H:%M')}")
        
        if hasattr(ctx, 'channel'):
            await ctx.channel.send(embed=embed)
        else:
            await ctx.followup.send(embed=embed, ephemeral=True)
    
    async def send_alert_summary(self, ctx):
        """Send alert summary"""
        alerts = self.data_store.data.get("alerts", [])[-10:]  # Last 10 alerts
        
        if not alerts:
            embed = discord.Embed(
                title="🔔 Recent Alerts",
                description="No alerts have been triggered recently.",
                color=0x00ff00
            )
        else:
            embed = discord.Embed(
                title="🔔 Recent Alerts",
                color=0xff6600
            )
            
            for alert in reversed(alerts):
                timestamp = datetime.datetime.fromisoformat(alert['timestamp'])
                embed.add_field(
                    name=f"{alert['type'].upper()} - {timestamp.strftime('%m/%d %H:%M')}",
                    value=f"{alert['message']}",
                    inline=False
                )
        
        if hasattr(ctx, 'channel'):
            await ctx.channel.send(embed=embed)
        else:
            await ctx.followup.send(embed=embed, ephemeral=True)
    
    async def send_config_info(self, ctx):
        """Send configuration info"""
        embed = discord.Embed(
            title="⚙️ Current Configuration",
            color=0x3498db
        )
        
        embed.add_field(
            name="Update Interval",
            value=f"{CONFIG['update_interval']}s",
            inline=True
        )
        
        embed.add_field(
            name="View Mode",
            value=CONFIG['view_mode'],
            inline=True
        )
        
        embed.add_field(
            name="Color Mode",
            value=CONFIG['color_mode'],
            inline=True
        )
        
        embed.add_field(
            name="Alerts Enabled",
            value="✅ Yes" if CONFIG['enable_alerts'] else "❌ No",
            inline=True
        )
        
        embed.add_field(
            name="Docker Monitoring",
            value="✅ Yes" if CONFIG['monitor_docker'] else "❌ No",
            inline=True
        )
        
        thresholds = "\n".join([
            f"CPU: {CONFIG['thresholds']['cpu']}%",
            f"Memory: {CONFIG['thresholds']['memory']}%",
            f"Disk: {CONFIG['thresholds']['disk']}%",
            f"Temp: {CONFIG['thresholds']['temperature']}°C"
        ])
        
        embed.add_field(
            name="Alert Thresholds",
            value=thresholds,
            inline=False
        )
        
        if CONFIG['monitor_services']:
            embed.add_field(
                name="Monitored Services",
                value="\n".join([f"• {s}" for s in CONFIG['monitor_services']]),
                inline=False
            )
        
        if hasattr(ctx, 'channel'):
            await ctx.channel.send(embed=embed)
        else:
            await ctx.followup.send(embed=embed, ephemeral=True)
    
    async def send_audit_logs(self, message):
        """Send audit logs"""
        logs = self.data_store.data.get("audit_logs", [])[-15:]  # Last 15 logs
        
        if not logs:
            embed = discord.Embed(
                title="📋 Audit Logs",
                description="No audit logs available.",
                color=0x95a5a6
            )
        else:
            embed = discord.Embed(
                title="📋 Audit Logs",
                color=0x3498db
            )
            
            log_text = ""
            for log in reversed(logs):
                timestamp = datetime.datetime.fromisoformat(log['timestamp'])
                status = "✅" if log['success'] else "❌"
                log_text += f"{status} `{timestamp.strftime('%m/%d %H:%M')}` - {log['user']}: {log['command']}\n"
            
            embed.description = log_text
        
        await message.channel.send(embed=embed)
    
    async def send_help(self, message):
        """Send help message"""
        embed = discord.Embed(
            title="📚 Bot Commands Help",
            description="Available commands for the server monitor bot",
            color=0x3498db
        )
        
        # Public commands
        public_cmds = {
            "!stats / !updatestats": "Show current server statistics",
            "!setstats": "Reset and create new stats message",
            "!history [hours]": "Show historical stats (default: 24h)",
            "!alerts": "Show recent alerts",
            "!help": "Show this help message"
        }
        
        public_text = "\n".join([f"**{cmd}**\n{desc}" for cmd, desc in public_cmds.items()])
        embed.add_field(name="👥 Public Commands", value=public_text, inline=False)
        
        # Admin commands
        admin_cmds = {
            "!config": "Show current configuration",
            "!config interval <seconds>": "Set update interval",
            "!config view <detailed/compact>": "Set view mode",
            "!config color <dynamic/static>": "Set color mode",
            "!config threshold <type> <value>": "Set alert threshold",
            "!config alerts <on/off>": "Enable/disable alerts",
            "!audit": "Show audit logs",
            "!service status <name>": "Check service status",
            "!service restart <name>": "Restart a service"
        }
        
        admin_text = "\n".join([f"**{cmd}**\n{desc}" for cmd, desc in admin_cmds.items()])
        embed.add_field(name="🔐 Admin Commands", value=admin_text, inline=False)
        
        embed.set_footer(text="Use the buttons on stats message for quick actions!")
        
        await message.channel.send(embed=embed)
    
    async def handle_config_command(self, message):
        """Handle config commands"""
        parts = message.content.lower().split()
        
        if len(parts) == 1:
            # Show config
            await self.send_config_info(message)
            return
        
        if len(parts) < 3:
            await message.reply("Usage: `!config <setting> <value>`")
            return
        
        setting = parts[1]
        value = " ".join(parts[2:])
        
        success = False
        response = ""
        
        try:
            if setting == "interval":
                interval = int(value)
                if 10 <= interval <= 300:
                    CONFIG["update_interval"] = interval
                    self.update_stats.change_interval(seconds=interval)
                    response = f"✅ Update interval set to {interval} seconds"
                    success = True
                else:
                    response = "❌ Interval must be between 10 and 300 seconds"
            
            elif setting == "view":
                if value in ["detailed", "compact"]:
                    CONFIG["view_mode"] = value
                    response = f"✅ View mode set to {value}"
                    success = True
                else:
                    response = "❌ View mode must be 'detailed' or 'compact'"
            
            elif setting == "color":
                if value in ["dynamic", "static"]:
                    CONFIG["color_mode"] = value
                    response = f"✅ Color mode set to {value}"
                    success = True
                else:
                    response = "❌ Color mode must be 'dynamic' or 'static'"
            
            elif setting == "threshold":
                if len(parts) >= 4:
                    threshold_type = parts[2]
                    threshold_value = float(parts[3])
                    
                    if threshold_type in CONFIG["thresholds"]:
                        CONFIG["thresholds"][threshold_type] = threshold_value
                        response = f"✅ {threshold_type} threshold set to {threshold_value}"
                        success = True
                    else:
                        response = f"❌ Unknown threshold type. Available: cpu, memory, disk, temperature"
                else:
                    response = "Usage: `!config threshold <type> <value>`"
            
            elif setting == "alerts":
                if value in ["on", "off"]:
                    CONFIG["enable_alerts"] = (value == "on")
                    response = f"✅ Alerts {'enabled' if value == 'on' else 'disabled'}"
                    success = True
                else:
                    response = "❌ Value must be 'on' or 'off'"
            
            else:
                response = f"❌ Unknown setting: {setting}"
            
            # Save config
            if success:
                self.save_config()
            
            # Log audit
            self.data_store.add_audit_log(
                str(message.author),
                f"config {setting} {value}",
                success
            )
            
            await message.reply(response)
            
        except ValueError:
            await message.reply("❌ Invalid value format")
        except Exception as e:
            await message.reply(f"❌ Error: {str(e)}")
    
    async def handle_service_command(self, message):
        """Handle service commands"""
        parts = message.content.split()
        
        if len(parts) < 3:
            await message.reply("Usage: `!service <status/restart> <service_name>`")
            return
        
        action = parts[1].lower()
        service_name = parts[2]
        
        if action == "status":
            status = self.get_service_status(service_name)
            emoji = "✅" if status["active"] else "❌"
            await message.reply(f"{emoji} Service **{service_name}**: {status['status']}")
            
            self.data_store.add_audit_log(
                str(message.author),
                f"service status {service_name}",
                True
            )
        
        elif action == "restart":
            # Add confirmation
            await message.reply(f"⚠️ Are you sure you want to restart **{service_name}**? Reply with `yes` to confirm.")
            
            def check(m):
                return m.author == message.author and m.channel == message.channel and m.content.lower() == 'yes'
            
            try:
                await self.client.wait_for('message', check=check, timeout=30.0)
                
                # Restart service
                try:
                    result = subprocess.run(
                        ['systemctl', 'restart', service_name],
                        capture_output=True,
                        text=True,
                        timeout=30
                    )
                    
                    if result.returncode == 0:
                        await message.channel.send(f"✅ Service **{service_name}** restarted successfully")
                        success = True
                    else:
                        await message.channel.send(f"❌ Failed to restart service: {result.stderr}")
                        success = False
                    
                    self.data_store.add_audit_log(
                        str(message.author),
                        f"service restart {service_name}",
                        success
                    )
                    
                except Exception as e:
                    await message.channel.send(f"❌ Error: {str(e)}")
                    self.data_store.add_audit_log(
                        str(message.author),
                        f"service restart {service_name}",
                        False
                    )
            
            except asyncio.TimeoutError:
                await message.channel.send("❌ Confirmation timeout. Restart cancelled.")
        
        else:
            await message.reply("❌ Unknown action. Use 'status' or 'restart'")
    
    def save_config(self):
        """Save current config to file"""
        try:
            config_to_save = CONFIG.copy()
            # Convert hex color to string for JSON
            config_to_save['embed_color'] = hex(config_to_save['embed_color'])
            
            with open('config.json', 'w') as f:
                json.dump(config_to_save, f, indent=4)
            print("Config saved to config.json")
        except Exception as e:
            print(f"Error saving config: {e}")
    
    async def send_or_update_stats(self):
        """Kirim stats baru atau update yang sudah ada"""
        try:
            channel = self.client.get_channel(CONFIG["channel_id"])
            if not channel:
                print("Channel tidak ditemukan!")
                return
            
            embed = await self.create_stats_embed()
            view = StatsView(self)
            
            # Store current stats in history
            cpu_info = self.get_cpu_info()
            memory_info = self.get_memory_info()
            disk_info = self.get_disk_info()
            
            self.data_store.add_history({
                "cpu": cpu_info['usage'],
                "memory": memory_info['percentage'],
                "disk": disk_info['percentage'],
                "temperature": cpu_info['temperature']
            })
            
            if self.status_message:
                # Update pesan yang sudah ada
                await self.status_message.edit(embed=embed, view=view)
                print(f"Stats updated at {datetime.datetime.now().strftime('%H:%M:%S')}")
            else:
                # Kirim pesan baru
                self.status_message = await channel.send(embed=embed, view=view)
                print("Stats message sent!")
                
        except Exception as e:
            print(f"Error updating stats: {e}")
    
    @tasks.loop(seconds=CONFIG["update_interval"])
    async def update_stats(self):
        """Loop untuk update otomatis"""
        await self.send_or_update_stats()
    
    @tasks.loop(seconds=60)
    async def check_alerts(self):
        """Loop untuk check alerts"""
        await self.check_threshold_alerts()
    
    @update_stats.before_loop
    async def before_update_stats(self):
        """Wait until bot is ready"""
        await self.client.wait_until_ready()
    
    @check_alerts.before_loop
    async def before_check_alerts(self):
        """Wait until bot is ready"""
        await self.client.wait_until_ready()
    
    def run(self):
        """Jalankan bot"""
        if CONFIG["token"] == "YOUR_BOT_TOKEN":
            print("Harap ganti TOKEN di konfigurasi!")
            return
        
        if CONFIG["channel_id"] == 0:
            print("Harap ganti CHANNEL_ID di konfigurasi!")
            return
        
        try:
            self.client.run(CONFIG["token"])
        except Exception as e:
            print(f"Error starting bot: {e}")

def load_config():
    """Load config from file if exists"""
    if os.path.exists('config.json'):
        try:
            with open('config.json', 'r') as f:
                loaded_config = json.load(f)
                
                # Convert embed_color from string to int if needed
                if 'embed_color' in loaded_config and isinstance(loaded_config['embed_color'], str):
                    if loaded_config['embed_color'].startswith('0x'):
                        loaded_config['embed_color'] = int(loaded_config['embed_color'], 16)
                    else:
                        loaded_config['embed_color'] = int(loaded_config['embed_color'])
                
                CONFIG.update(loaded_config)
                print("Config loaded from config.json")
        except Exception as e:
            print(f"Error loading config.json: {e}")

def create_sample_config():
    """Create sample config file"""
    sample_config = {
        "token": "YOUR_BOT_TOKEN",
        "channel_id": 0,
        "update_interval": 30,
        "embed_color": "0x00ff00",
        "admin_role_ids": [],
        "admin_user_ids": [],
        "alert_channel_id": 0,
        "thresholds": {
            "cpu": 80,
            "memory": 85,
            "disk": 90,
            "temperature": 75
        },
        "view_mode": "detailed",
        "color_mode": "dynamic",
        "enable_alerts": True,
        "monitor_docker": False,
        "monitor_services": []
    }
    
    with open('config.json.example', 'w') as f:
        json.dump(sample_config, f, indent=4)
    
    print("Created config.json.example - Copy to config.json and edit!")

if __name__ == "__main__":
    print("=" * 50)
    print("Discord Server Monitor Bot - Enhanced Edition")
    print("=" * 50)
    
    # Load config if exists
    load_config()
    
    # Create sample config if not exists
    if not os.path.exists('config.json.example'):
        create_sample_config()
    
    # Create and run bot
    monitor = ServerMonitor()
    monitor.run()
