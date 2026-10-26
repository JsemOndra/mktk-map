"""Guess the service from the port (and the domain, if known)."""

from i18n import t

PORTS = {
    ("tcp", 20): "FTP data", ("tcp", 21): "FTP", ("tcp", 22): "SSH", ("tcp", 23): "Telnet",
    ("tcp", 25): "SMTP", ("tcp", 53): "DNS", ("udp", 53): "DNS", ("udp", 67): "DHCP",
    ("udp", 68): "DHCP", ("tcp", 80): "HTTP", ("udp", 123): "NTP", ("tcp", 110): "POP3",
    ("tcp", 143): "IMAP", ("udp", 137): "NetBIOS", ("udp", 138): "NetBIOS", ("tcp", 139): "SMB",
    ("udp", 161): "SNMP", ("tcp", 179): "BGP", ("tcp", 389): "LDAP", ("tcp", 443): "HTTPS",
    ("udp", 443): "QUIC (HTTP/3)", ("tcp", 445): "SMB", ("tcp", 465): "SMTPS",
    ("udp", 500): "IPsec IKE", ("tcp", 554): "RTSP", ("udp", 554): "RTSP", ("tcp", 587): "SMTP submit",
    ("tcp", 636): "LDAPS", ("tcp", 853): "DNS-over-TLS", ("udp", 853): "DNS-over-QUIC",
    ("tcp", 993): "IMAPS", ("tcp", 995): "POP3S", ("udp", 1194): "OpenVPN", ("tcp", 1194): "OpenVPN",
    ("udp", 1900): "SSDP/UPnP", ("tcp", 1883): "MQTT", ("tcp", 8883): "MQTT/TLS",
    ("tcp", 1935): "RTMP", ("tcp", 3074): "Xbox Live", ("udp", 3074): "Xbox Live",
    ("tcp", 3389): "RDP", ("udp", 3478): "STUN/TURN", ("tcp", 3478): "STUN/TURN",
    ("udp", 3479): "STUN", ("udp", 3480): "STUN", ("udp", 4500): "IPsec NAT-T",
    ("tcp", 5222): "XMPP/WhatsApp", ("tcp", 5223): "Apple Push", ("tcp", 5228): "Google Play/FCM",
    ("udp", 5353): "mDNS", ("tcp", 5671): "AMQPS (Azure IoT)", ("udp", 5683): "CoAP/CoIoT",
    ("tcp", 6012): "Shelly cloud", ("tcp", 6022): "Shelly cloud", ("tcp", 6881): "BitTorrent",
    ("udp", 6881): "BitTorrent", ("tcp", 8008): "Chromecast", ("tcp", 8009): "Chromecast",
    ("tcp", 8080): "HTTP alt", ("tcp", 8123): "Home Assistant", ("tcp", 8291): "Winbox",
    ("tcp", 8443): "HTTPS alt", ("tcp", 8728): "RouterOS API", ("tcp", 8729): "RouterOS API-SSL",
    ("tcp", 9000): "HTTP alt", ("udp", 19302): "Google STUN (Meet/Duo)",
    ("udp", 27015): "Steam", ("tcp", 27015): "Steam", ("udp", 51820): "WireGuard",
    ("tcp", 4070): "Spotify", ("tcp", 5938): "TeamViewer", ("udp", 5938): "TeamViewer",
    ("udp", 10000): "Jitsi/WebRTC", ("tcp", 1723): "PPTP", ("udp", 1701): "L2TP",
    ("tcp", 9993): "ZeroTier", ("udp", 9993): "ZeroTier", ("udp", 41641): "Tailscale",
    ("tcp", 7000): "AirPlay", ("tcp", 62078): "iPhone sync", ("tcp", 2049): "NFS",
    ("tcp", 5000): "Synology/UPnP", ("tcp", 5001): "Synology HTTPS", ("tcp", 32400): "Plex",
    ("tcp", 8006): "Proxmox", ("tcp", 9100): "tisk (RAW)", ("tcp", 631): "IPP tisk",
}

# domain suffixes -> more specific service (for HTTPS/QUIC)
DOMAINS = [
    ("googlevideo.com", "YouTube"), ("youtube.com", "YouTube"), ("ytimg.com", "YouTube"),
    ("nflxvideo.net", "Netflix"), ("netflix.com", "Netflix"), ("spotify.com", "Spotify"),
    ("scdn.co", "Spotify"), ("whatsapp.net", "WhatsApp"), ("whatsapp.com", "WhatsApp"),
    ("fbcdn.net", "Facebook"), ("facebook.com", "Facebook"), ("instagram.com", "Instagram"),
    ("cdninstagram.com", "Instagram"), ("tiktokcdn.com", "TikTok"), ("tiktokv.com", "TikTok"),
    ("icloud.com", "iCloud"), ("apple.com", "Apple"), ("mzstatic.com", "Apple"),
    ("windowsupdate.com", "Windows Update"), ("microsoft.com", "Microsoft"),
    ("office.com", "Microsoft 365"), ("live.com", "Microsoft"), ("teams.microsoft.com", "Teams"),
    ("azure-devices.net", "Azure IoT Hub"), ("servicebus.windows.net", "Azure Service Bus"),
    ("amazonaws.com", "AWS"), ("cloudfront.net", "CloudFront CDN"), ("akamaized.net", "Akamai CDN"),
    ("googleapis.com", "Google API"), ("gstatic.com", "Google"), ("google.com", "Google"),
    ("1e100.net", "Google"), ("shelly.cloud", "Shelly cloud"), ("tuya", "Tuya"),
    ("home-assistant.io", "Home Assistant"), ("nabucasa.com", "HA Cloud"),
    ("steamcontent.com", "Steam"), ("steampowered.com", "Steam"), ("discord.gg", "Discord"),
    ("discord.com", "Discord"), ("discordapp.com", "Discord"), ("twitch.tv", "Twitch"),
    ("ttvnw.net", "Twitch"), ("seznam.cz", "Seznam"), ("szn.cz", "Seznam"),
    ("mikrotik.com", "MikroTik"), ("bambulab.com", "Bambu Lab"), ("github.com", "GitHub"),
    ("anthropic.com", "Anthropic"), ("claude.ai", "Claude"), ("openai.com", "OpenAI"),
    ("ubnt.com", "Ubiquiti"), ("ntp.org", "NTP"), ("dropbox.com", "Dropbox"),
]


def guess(proto: str | None, port, name: str | None = None) -> str | None:
    proto = (proto or "").lower()
    try:
        p = int(port) if port not in (None, "") else None
    except (TypeError, ValueError):
        p = None
    by_port = PORTS.get((proto, p)) if p is not None else None
    if name:
        n = name.lower().rstrip(".")
        for suffix, svc in DOMAINS:
            if n == suffix or n.endswith("." + suffix) or (("." not in suffix) and suffix in n):
                if by_port in (None, "HTTPS", "HTTP", "QUIC (HTTP/3)", "HTTPS alt"):
                    return f"{svc} ({by_port})" if by_port else svc
                return by_port
    if by_port:
        return by_port
    if proto == "icmp":
        return "ICMP (ping)"
    if p is not None and p >= 49152:
        return t("srv.dynamic_port")
    return None
