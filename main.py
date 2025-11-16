import discord
from discord.ext import tasks
import psutil
import platform
import asyncio
import datetime
from typing import Optional
import json
import os
import time

# ===== KONFIGURASI =====
CONFIG = {
    "token": "YOUR_BOT_TOKEN",  # Ganti dengan token bot Discord Anda
    "channel_id": 0,  # Ganti dengan ID channel (angka)
    "update_interval": 30,  # Update setiap 30 detik
    "embed_color": 0x00ff00,  # Warna hijau
}

class NetworkMonitor:
    def __init__(self):
        self.last_bytes_sent = 0
        self.last_bytes_recv = 0
        self.last_check_time = time.time()
        self.current_sent_rate = 0
        self.current_recv_rate = 0
        
    def update_rates(self, bytes_sent, bytes_recv):
        current_time = time.time()
        time_diff = current_time - self.last_check_time
        
        if self.last_bytes_sent > 0 and time_diff > 0:
            sent_diff = bytes_sent - self.last_bytes_sent
            recv_diff = bytes_recv - self.last_bytes_recv
            
            self.current_sent_rate = sent_diff / time_diff / 1024  # KB/s
            self.current_recv_rate = recv_diff / time_diff / 1024  # KB/s
        
        self.last_bytes_sent = bytes_sent
        self.last_bytes_recv = bytes_recv
        self.last_check_time = current_time

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
        
        # Setup events
        self.setup_events()
    
    def setup_events(self):
        @self.client.event
        async def on_ready():
            print(f'Bot logged in as {self.client.user}!')
            print(f'Starting auto-update every {CONFIG["update_interval"]} seconds...')
            
            # Start the monitoring loop
            self.update_stats.start()
        
        @self.client.event
        async def on_message(message):
            if message.author.bot:
                return
            
            # Manual update command
            if message.content.lower() == '!updatestats':
                await self.send_or_update_stats()
                await message.add_reaction('✅')
            
            # Reset stats message
            elif message.content.lower() == '!setstats':
                self.status_message = None
                await self.send_or_update_stats()
                await message.add_reaction('🔄')
    
    def get_cpu_info(self) -> dict:
        """Mendapatkan informasi CPU"""
        try:
            # Get CPU model from /proc/cpuinfo on Linux
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
            
            return {
                "model": cpu_model,
                "usage": cpu_usage,
                "cores_physical": cpu_count_physical or cpu_count_logical // 2,
                "cores_logical": cpu_count_logical,
                "frequency": cpu_freq.current if cpu_freq else "N/A"
            }
        except Exception as e:
            print(f"Error getting CPU info: {e}")
            return {
                "model": "Unknown Processor",
                "usage": 0,
                "cores_physical": 1,
                "cores_logical": 2,
                "frequency": "N/A"
            }
    
    def get_memory_info(self) -> dict:
        """Mendapatkan informasi Memory"""
        try:
            memory = psutil.virtual_memory()
            
            return {
                "total": memory.total / (1024**3),  # GB
                "used": memory.used / (1024**3),   # GB
                "available": memory.available / (1024**3),  # GB
                "percentage": memory.percent
            }
        except Exception as e:
            print(f"Error getting memory info: {e}")
            return {
                "total": 0,
                "used": 0,
                "available": 0,
                "percentage": 0
            }
    
    def get_disk_info(self) -> dict:
        """Mendapatkan informasi Disk"""
        try:
            disk = psutil.disk_usage('/')
            total_gb = disk.total / (1024**3)
            used_gb = disk.used / (1024**3)
            
            # Convert to TB if larger than 1000 GB
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
                "used_display": used_display
            }
        except:
            # Fallback untuk Windows
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
                    "used_display": used_display
                }
            except Exception as e:
                print(f"Error getting disk info: {e}")
                return {
                    "total": 0,
                    "used": 0,
                    "free": 0,
                    "percentage": 0,
                    "total_display": "0 GB",
                    "used_display": "0 GB"
                }
    
    def get_network_info(self) -> dict:
        """Mendapatkan informasi Network dengan current rates"""
        try:
            # Get network I/O statistics
            net_io = psutil.net_io_counters()
            
            # Update current rates
            self.network_monitor.update_rates(net_io.bytes_sent, net_io.bytes_recv)
            
            # Format total bytes
            total_sent = self.format_bytes_network(net_io.bytes_sent)
            total_recv = self.format_bytes_network(net_io.bytes_recv)
            
            return {
                "bytes_sent": net_io.bytes_sent,
                "bytes_recv": net_io.bytes_recv,
                "current_sent": f"{self.network_monitor.current_sent_rate:.2f} KB/s",
                "current_recv": f"{self.network_monitor.current_recv_rate:.2f} KB/s",
                "total_sent": total_sent,
                "total_recv": total_recv,
            }
        except Exception as e:
            print(f"Error getting network info: {e}")
            return {
                "bytes_sent": 0,
                "bytes_recv": 0,
                "current_sent": "0.00 KB/s",
                "current_recv": "0.00 KB/s",
                "total_sent": "0 B",
                "total_recv": "0 B",
            }
    
    def format_bytes_network(self, bytes_value: int) -> str:
        """Format bytes untuk network stats"""
        if bytes_value >= 1024**4:  # TB
            return f"{bytes_value / (1024**4):.2f} TB"
        elif bytes_value >= 1024**3:  # GB
            return f"{bytes_value / (1024**3):.2f} GB"
        elif bytes_value >= 1024**2:  # MB
            return f"{bytes_value / (1024**2):.2f} MB"
        elif bytes_value >= 1024:  # KB
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
            
            return f"{days} days, {hours} hours, {minutes} minutes, {seconds} seconds"
        except Exception as e:
            print(f"Error getting uptime: {e}")
            return "N/A"
    
    async def get_discord_ping(self) -> int:
        """Mendapatkan ping ke Discord"""
        return round(self.client.latency * 1000)
    
    async def create_stats_embed(self) -> discord.Embed:
        """Membuat embed dengan statistik server persis seperti screenshot"""
        cpu_info = self.get_cpu_info()
        memory_info = self.get_memory_info()
        disk_info = self.get_disk_info()
        network_info = self.get_network_info()
        uptime = self.get_uptime()
        discord_ping = await self.get_discord_ping()
        
        # Create embed dengan layout yang persis sama
        embed = discord.Embed(
            title="Server Stats",
            color=CONFIG["embed_color"]
        )
        
        # Server Info section
        server_info = f"**Server Info**\n\n"
        server_info += f"CPU: {cpu_info['model']}\n"
        server_info += f"CPU Usage: {cpu_info['usage']:.2f}%\n\n"
        server_info += f"Cores (Physical): {cpu_info['cores_physical']}\n"
        server_info += f"Cores (Total): {cpu_info['cores_logical']}\n"
        server_info += "─" * 47 + "\n"
        server_info += f"Total Devices: 1\n"
        server_info += f"Current Usage: {disk_info['used']:.2f}/{disk_info['total']:.2f} GB\n\n"
        server_info += f"Memory Usage (w/ buffers): {memory_info['used']:.2f} GB\n"
        server_info += f"Available: {memory_info['available']:.2f} GB\n"
        server_info += "─" * 47 + "\n"
        server_info += f"Disk Usage: {disk_info['used_display']}/{disk_info['total_display']}\n"
        server_info += "─" * 47 + "\n"
        server_info += f"Network Stats:\n\n"
        server_info += f"Current Transfer: {network_info['current_sent']}\n"
        server_info += f"Current Received: {network_info['current_recv']}\n\n"
        server_info += f"Total Transferred: {network_info['total_sent']}\n"
        server_info += f"Total Received: {network_info['total_recv']}\n"
        server_info += "─" * 47 + "\n"
        server_info += f"Uptime: {uptime}"
        
        embed.description = server_info
        
        # Discord API ping sebagai field terpisah
        embed.add_field(
            name="Discord API websocket ping",
            value=f"{discord_ping} ms",
            inline=False
        )
        
        # Footer dengan timestamp yang sama formatnya
        now = datetime.datetime.now()
        embed.set_footer(
            text=f"Updated at | Today at {now.strftime('%H:%M')}"
        )
        
        return embed
    
    async def send_or_update_stats(self):
        """Kirim stats baru atau update yang sudah ada"""
        try:
            channel = self.client.get_channel(CONFIG["channel_id"])
            if not channel:
                print("Channel tidak ditemukan!")
                return
            
            embed = await self.create_stats_embed()
            
            if self.status_message:
                # Update pesan yang sudah ada
                await self.status_message.edit(embed=embed)
                print(f"Stats updated at {datetime.datetime.now().strftime('%H:%M:%S')}")
            else:
                # Kirim pesan baru
                self.status_message = await channel.send(embed=embed)
                print("Stats message sent!")
                
        except Exception as e:
            print(f"Error updating stats: {e}")
    
    @tasks.loop(seconds=CONFIG["update_interval"])
    async def update_stats(self):
        """Loop untuk update otomatis"""
        await self.send_or_update_stats()
    
    @update_stats.before_loop
    async def before_update_stats(self):
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
        "embed_color": "0x00ff00"
    }
    
    with open('config.json.example', 'w') as f:
        json.dump(sample_config, f, indent=4)
    
    print("Created config.json.example - Copy to config.json and edit!")

if __name__ == "__main__":
    print("Starting Discord Server Monitor Bot...")
    
    # Load config if exists
    load_config()
    
    # Create sample config if not exists
    if not os.path.exists('config.json.example'):
        create_sample_config()
    
    # Create and run bot
    monitor = ServerMonitor()
    monitor.run()
