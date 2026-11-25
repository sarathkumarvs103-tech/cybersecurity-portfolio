# 01 – Security & Network Lab (08/2026 – today)

A small company network ("KUTTU Enterprise") built at home to practise system administration, network security and log monitoring.

## Setup
- **Server:** Dell PowerEdge R450 (Intel Xeon Silver, 128 GB RAM, 10 GbE NIC) running **VMware ESXi 7** and **vCenter**
- **Firewall / gateway:** **OPNsense** on bare metal – DHCP for the lab, DNS forwarded to the domain controller
- **Switching:** TP-Link managed switch
- **Segmentation:** 8 network segments by department – Management, Servers, Domain Controllers, IT, Finance, Sales, Employees, Guests (with a VLAN addressing plan)
- **Directory:** **Windows Server 2022 Active Directory** – DC01, 5 domain-joined Windows 10/11 clients, 10 users, group policies (e.g. hide last signed-in user)
- **Monitoring:** central log analysis with **Splunk**
- **Always-on Raspberry Pi 4:** network file server, **Pi-hole** DNS ad & tracker blocking, secure remote access to the home network via **Tailscale** (WireGuard-based VPN)

## Skills shown
Network segmentation · firewall administration · Active Directory & GPOs · virtualisation · DNS/DHCP · VPN · SIEM log analysis · documentation
