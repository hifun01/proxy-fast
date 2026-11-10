set -Eeuo pipefail
export LC_ALL=C
umask 077
CORE_VERSION=1.14.4
BASE=/etc/proxy-fast
LIB=/usr/local/lib/proxy-fast
PUBLIC=/var/lib/proxy-fast/public
CORE=$LIB/sing-box
START=$SECONDS
STEP=preflight
TMP=
log() { printf '[%ss] %s\n' "$((SECONDS-START))" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }
cleanup() { [[ -z $TMP ]] || rm -rf -- "$TMP"; }
trap cleanup EXIT
trap 'log "FAILED at $STEP (line $LINENO). Fix the error and rerun; credentials are retained. Logs: journalctl -u proxy-fast -u proxy-fast-sub -n 30 --no-pager"' ERR

[[ ${EUID:-1} == 0 ]] || die 'Run as root: sudo bash proxy-fast.sh'
case ${1:-install} in install|status) ACTION=${1:-install};; *) die 'Usage: sudo bash proxy-fast.sh [install|status]';; esac
if [[ $ACTION == status ]]; then
    [[ -f $BASE/result.json ]] || die 'Not installed.'
    systemctl is-active --quiet proxy-fast && systemctl is-active --quiet proxy-fast-sub || die 'One or both services are stopped.'
    python3 - "$BASE/result.json" <<'PY'
import json,sys
r=json.load(open(sys.argv[1]))
for k in ('mihomo','sing_box'): print(f'{k}: {r[k]}')
print('ports: TCP 443,8080; cloud security group must allow these ports')
PY
    exit
fi
[[ $(uname -s) == Linux && -d /run/systemd/system ]] || die 'Requires Linux with running systemd (Ubuntu/Debian).'
OS_ID=$(source /etc/os-release; printf '%s' "$ID")
case $OS_ID in ubuntu|debian) ;; *) die 'This first version supports Ubuntu/Debian only.';; esac
case $(uname -m) in
    x86_64) ARCH=amd64; HASH=699a1b510aa4396b2176d32d1181920399ebf1e267835da6f3b598d0f7c24968;;
    aarch64|arm64) ARCH=arm64; HASH=436441f76ee2f7fe21b5b2d426d3cb261a480455a704865789ad533e4273ec26;;
    *) die 'Requires x86_64 or ARM64.';;
esac
command -v flock >/dev/null || die 'flock is required (util-linux package).'
exec 9>/run/proxy-fast.lock
flock -n 9 || die 'Another installation is running.'
STEP=dependencies
NEED=()
command -v curl >/dev/null || NEED+=(curl)
command -v python3 >/dev/null || NEED+=(python3)
command -v tar >/dev/null || NEED+=(tar)
[[ -s /etc/ssl/certs/ca-certificates.crt ]] || NEED+=(ca-certificates)
if ((${#NEED[@]})); then
    log "Installing missing dependencies only: ${NEED[*]}"
    export DEBIAN_FRONTEND=noninteractive
    apt-get -o DPkg::Lock::Timeout=180 update -qq
    apt-get -o DPkg::Lock::Timeout=180 install -y --no-install-recommends "${NEED[@]}"
fi
mkdir -p "$BASE" "$LIB" "$PUBLIC"
chmod 700 "$BASE"
chmod 755 "$LIB" /var/lib/proxy-fast "$PUBLIC"
TMP=$(mktemp -d /tmp/proxy-fast.XXXXXXXX)
STEP=download
if [[ ! -x $CORE ]] || [[ $("$CORE" version 2>/dev/null | head -n 1) != "sing-box version $CORE_VERSION" ]]; then
    log "Downloading official sing-box $CORE_VERSION ($ARCH)"
    URL="https://github.com/SagerNet/sing-box/releases/download/v$CORE_VERSION/sing-box-$CORE_VERSION-linux-$ARCH.tar.gz"
    curl --fail --location --retry 2 --connect-timeout 10 --max-time 180 "$URL" -o "$TMP/core.tar.gz"
    printf '%s  %s\n' "$HASH" "$TMP/core.tar.gz" | sha256sum --check --status || die 'Core archive checksum mismatch.'
    tar -xzf "$TMP/core.tar.gz" -C "$TMP" "sing-box-$CORE_VERSION-linux-$ARCH/sing-box"
    install -m 755 "$TMP/sing-box-$CORE_VERSION-linux-$ARCH/sing-box" "$CORE.new"
    mv -f "$CORE.new" "$CORE"
else
    log 'Using cached core; no download or package refresh.'
fi
log 'Core ready.'

# Kept in this single file so the first VPS test needs only one upload.
cat > "$LIB/runtime.py.new" <<'PY'
import http.server, ipaddress, json, os, secrets, socket, subprocess, sys, tempfile, time, urllib.parse, urllib.request, uuid
from pathlib import Path

def write_json(path, value, mode=0o600):
    path=Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(value,f,ensure_ascii=False,indent=2); f.write('\n')
        os.chmod(tmp,mode); os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def local_url(url,method='GET'):
    # Bypass inherited proxies for metadata and local health checks only.
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
        urllib.request.Request(url,method=method),timeout=3)

def public_ip():
    def valid(value):
        addr=ipaddress.ip_address(value.strip())
        if addr.version!=4 or not addr.is_global: raise ValueError('Public IPv4 required')
        return str(addr)
    # IMDSv2 on Alibaba ECS avoids external IP-lookup services.
    try:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req=urllib.request.Request('http://100.100.100.200/latest/api/token',method='PUT',
            headers={'X-aliyun-ecs-metadata-token-ttl-seconds':'60'})
        token=opener.open(req,timeout=1).read().decode()
        for key in ('public-ipv4','eipv4'):
            try:
                req=urllib.request.Request('http://100.100.100.200/latest/meta-data/'+key,
                    headers={'X-aliyun-ecs-metadata-token':token})
                return valid(opener.open(req,timeout=1).read().decode())
            except Exception: pass
    except Exception: pass
    for url in ('https://api.ipify.org','https://checkip.amazonaws.com'):
        try:
            with local_url(url) as response: return valid(response.read(128).decode())
        except Exception: pass
    raise RuntimeError('Cannot determine public IPv4; check outbound networking.')

def render(base,public,core,ip=None):
    base,public=Path(base),Path(public)
    state_path=base/'state.json'
    if state_path.exists():
        state=json.loads(state_path.read_text())
    else:
        output=subprocess.check_output([core,'generate','reality-keypair'],text=True)
        keys=dict(line.split(':',1) for line in output.splitlines() if ':' in line)
        state={'uuid':str(uuid.uuid4()),'private_key':keys['PrivateKey'].strip(),
            'public_key':keys['PublicKey'].strip(),'short_id':secrets.token_hex(4),
            'token':secrets.token_urlsafe(32),'sni':'apple.com','port':443,'sub_port':8080}
        write_json(state_path,state)
    ip=ip or public_ip()
    addr=ipaddress.ip_address(ip)
    if addr.version!=4: raise ValueError('IPv4 only in this first version')
    # Minimal VLESS REALITY setup; optional protocols removed.
    server={'log':{'level':'warn','timestamp':True},'inbounds':[{
        'type':'vless','tag':'vless-in','listen':'0.0.0.0','listen_port':state['port'],
        'users':[{'uuid':state['uuid'],'flow':'xtls-rprx-vision'}],
        'tls':{'enabled':True,'server_name':state['sni'],'reality':{'enabled':True,
            'handshake':{'server':state['sni'],'server_port':443},
            'private_key':state['private_key'],'short_id':[state['short_id']]}}}],
        'outbounds':[{'type':'direct','tag':'direct'}]}
    write_json(base/'server.json',server)
    outbound={'type':'vless','tag':'node','server':ip,'server_port':state['port'],
        'uuid':state['uuid'],'flow':'xtls-rprx-vision','tls':{'enabled':True,
            'server_name':state['sni'],'utls':{'enabled':True,'fingerprint':'chrome'},
            'reality':{'enabled':True,'public_key':state['public_key'],'short_id':state['short_id']}}}
    # Full remote profile: TUN for GUI/mobile clients plus a loopback mixed port.
    client={'log':{'level':'warn'},'dns':{'servers':[{'type':'https','tag':'remote',
        'server':'1.1.1.1','detour':'proxy'}],'final':'remote','strategy':'ipv4_only'},
        'inbounds':[{'type':'tun','tag':'tun-in','address':['172.19.0.1/30'],
            'auto_route':True,'strict_route':True},
            {'type':'mixed','tag':'mixed-in','listen':'127.0.0.1','listen_port':7890}],
        'outbounds':[{'type':'selector','tag':'proxy','outbounds':['node'],'default':'node'},
            outbound,{'type':'direct','tag':'direct'}],
        'route':{'auto_detect_interface':True,'rules':[{'action':'sniff'},
            {'protocol':'dns','action':'hijack-dns'},{'ip_is_private':True,'outbound':'direct'}],
            'final':'proxy'}}
    proxy={'name':'proxy-fast','type':'vless','server':ip,'port':state['port'],
        'uuid':state['uuid'],'network':'tcp','udp':True,'tls':True,
        'servername':state['sni'],'flow':'xtls-rprx-vision','client-fingerprint':'chrome',
        'reality-opts':{'public-key':state['public_key'],'short-id':state['short_id']}}
    # JSON is valid YAML 1.2, understood by Mihomo; avoids installing a YAML library.
    mihomo={'mixed-port':7890,'allow-lan':False,'mode':'rule','log-level':'warning',
        'proxies':[proxy],'proxy-groups':[{'name':'PROXY','type':'select','proxies':['proxy-fast']}],
        'rules':['IP-CIDR,127.0.0.0/8,DIRECT','IP-CIDR,10.0.0.0/8,DIRECT',
            'IP-CIDR,172.16.0.0/12,DIRECT','IP-CIDR,192.168.0.0/16,DIRECT','MATCH,PROXY']}
    params=urllib.parse.urlencode({'encryption':'none','security':'reality','type':'tcp',
        'sni':state['sni'],'fp':'chrome','pbk':state['public_key'],'sid':state['short_id'],
        'flow':'xtls-rprx-vision'})
    vless=f"vless://{state['uuid']}@{ip}:{state['port']}?{params}#proxy-fast"
    public.mkdir(parents=True,exist_ok=True)
    write_json(public/'clmi.yaml',mihomo,0o644)
    write_json(public/'sbox.json',client,0o644)
    write_json(public/'routes.json',{'token':state['token'],'port':state['sub_port']},0o644)
    url=f"http://{ip}:{state['sub_port']}/{state['token']}"
    result={'mihomo':url+'/clmi.yaml','sing_box':url+'/sbox.json','vless':vless,
        'public_ipv4':ip,'inbound_tcp':[state['port'],state['sub_port']],
        'outbound':'Internet TCP/UDP as needed; DNS resolution required',
        'core_version':'1.14.4','sni':state['sni']}
    write_json(base/'result.json',result)

def serve(public):
    public=Path(public)
    info=json.loads((public/'routes.json').read_text())
    routes={f"/{info['token']}/clmi.yaml":('clmi.yaml','application/yaml'),
        f"/{info['token']}/sbox.json":('sbox.json','application/json')}
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version='Subscription'; sys_version=''
        def setup(self):
            super().setup(); self.connection.settimeout(5)
        def log_message(self,*args): pass  # Keep subscription tokens out of logs.
        def do_HEAD(self): self.respond(False)
        def do_GET(self): self.respond(True)
        def respond(self,body):
            route=routes.get(urllib.parse.urlsplit(self.path).path)
            if route is None: self.send_error(404); return
            filename,mime=route
            data=(public/filename).read_bytes()
            self.send_response(200)
            self.send_header('Content-Type',mime+'; charset=utf-8')
            self.send_header('Content-Length',str(len(data)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers()
            if body: self.wfile.write(data)
    class Server(http.server.ThreadingHTTPServer):
        daemon_threads=True
    Server(('0.0.0.0',info['port']),Handler).serve_forever()

def probe(base):
    state=json.loads((Path(base)/'state.json').read_text())
    for port in (state['port'],state['sub_port']):
        with socket.create_connection(('127.0.0.1',port),timeout=2): pass
    for filename in ('clmi.yaml','sbox.json'):
        with local_url(f"http://127.0.0.1:{state['sub_port']}/{state['token']}/{filename}") as r:
            json.load(r)

def verify_proxy(base,public,core):
    outbound=json.loads((Path(public)/'sbox.json').read_text())['outbounds'][1]
    outbound['server']='127.0.0.1'
    with socket.socket() as s:
        s.bind(('127.0.0.1',0)); port=s.getsockname()[1]
    config={'log':{'level':'error'},'inbounds':[{'type':'mixed','listen':'127.0.0.1',
        'listen_port':port}],'outbounds':[outbound]}
    with tempfile.TemporaryDirectory(prefix='proxy-fast-check-') as tmp:
        path=Path(tmp)/'client.json'; write_json(path,config)
        proc=subprocess.Popen([core,'run','-c',str(path)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            ready=False
            for _ in range(30):
                if proc.poll() is not None: break
                try:
                    with socket.create_connection(('127.0.0.1',port),timeout=.2): ready=True
                    break
                except OSError: time.sleep(.1)
            if not ready: raise RuntimeError('Local verification client did not start')
            result=subprocess.run(['curl','--fail','--silent','--show-error','--noproxy','',
                '--proxy',f'http://127.0.0.1:{port}','--connect-timeout','5','--max-time','12',
                '--output',os.devnull,'https://www.gstatic.com/generate_204'],capture_output=True,text=True)
            if result.returncode: raise RuntimeError('Proxy egress check failed: '+result.stderr.strip())
        finally:
            proc.terminate()
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait()

if __name__=='__main__':
    try:
        if sys.argv[1]=='render': render(*sys.argv[2:])
        elif sys.argv[1]=='serve': serve(*sys.argv[2:])
        elif sys.argv[1]=='probe': probe(*sys.argv[2:])
        elif sys.argv[1]=='verify-proxy': verify_proxy(*sys.argv[2:])
        else: raise ValueError('Unknown action')
    except Exception as exc:
        print(f'ERROR: {exc}',file=sys.stderr); sys.exit(1)
PY
chmod 755 "$LIB/runtime.py.new"
mv -f "$LIB/runtime.py.new" "$LIB/runtime.py"

STEP=port-preflight
# Refuse to take ports from unrelated services. A prior successful install is reusable.
python3 - <<'PY'
import socket,subprocess
for port,unit in ((443,'proxy-fast'),(8080,'proxy-fast-sub')):
    if subprocess.run(['systemctl','is-active','--quiet',unit]).returncode==0: continue
    with socket.socket() as s:
        try: s.bind(('0.0.0.0',port))
        except OSError: raise SystemExit(f'TCP {port} is already in use; no existing service was stopped.')
PY
STEP=configuration
python3 "$LIB/runtime.py" render "$BASE" "$PUBLIC" "$CORE"
"$CORE" check -c "$BASE/server.json"
"$CORE" check -c "$PUBLIC/sbox.json"
log 'Configurations checked; credentials and subscription token persist across reruns.'

STEP=firewall
# Touch only the two required input rules, never flush rules or change output policy.
if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
    ufw allow 443/tcp comment 'proxy-fast'
    ufw allow 8080/tcp comment 'proxy-fast-sub'
elif command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
    for port in 443 8080; do
        firewall-cmd --add-port="$port/tcp"
        firewall-cmd --permanent --add-port="$port/tcp"
    done
elif command -v iptables >/dev/null; then
    for port in 443 8080; do
        iptables -C INPUT -p tcp --dport "$port" -m comment --comment proxy-fast -j ACCEPT 2>/dev/null ||
            iptables -I INPUT 1 -p tcp --dport "$port" -m comment --comment proxy-fast -j ACCEPT
    done
    # Reapply only our rules after reboot; do not install/overwrite firewall persistence.
    cat > /etc/systemd/system/proxy-fast-firewall.service <<EOF
[Unit]
Description=proxy-fast input ports
After=network.target
Before=proxy-fast.service proxy-fast-sub.service
[Service]
Type=oneshot
ExecStart=/bin/sh -c '$(command -v iptables) -C INPUT -p tcp --dport 443 -m comment --comment proxy-fast -j ACCEPT 2>/dev/null || $(command -v iptables) -I INPUT 1 -p tcp --dport 443 -m comment --comment proxy-fast -j ACCEPT'
ExecStart=/bin/sh -c '$(command -v iptables) -C INPUT -p tcp --dport 8080 -m comment --comment proxy-fast -j ACCEPT 2>/dev/null || $(command -v iptables) -I INPUT 1 -p tcp --dport 8080 -m comment --comment proxy-fast -j ACCEPT'
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable proxy-fast-firewall >/dev/null
elif command -v nft >/dev/null && nft list ruleset 2>/dev/null | grep -q 'hook input'; then
    die 'Custom nftables input rules are unsupported in this first version. Use a fresh Ubuntu/Debian VPS; existing rules were not rewritten.'
fi
log 'Host input ports handled. Existing output restrictions are retained.'
STEP=services
cat > /etc/systemd/system/proxy-fast.service <<EOF
[Unit]
Description=proxy-fast VLESS REALITY
Wants=network-online.target
After=network-online.target
[Service]
Type=simple
ExecStart=$CORE run -c $BASE/server.json
Restart=on-failure
RestartSec=2
LimitNOFILE=65536
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/proxy-fast-sub.service <<EOF
[Unit]
Description=proxy-fast subscription files
After=network.target
[Service]
Type=simple
DynamicUser=yes
ExecStart=/usr/bin/python3 $LIB/runtime.py serve $PUBLIC
Restart=on-failure
RestartSec=2
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
MemoryMax=96M
TasksMax=32
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable proxy-fast proxy-fast-sub >/dev/null
systemctl restart proxy-fast proxy-fast-sub
STEP=health-check
READY=0
for attempt in {1..20}; do
    if systemctl is-active --quiet proxy-fast && systemctl is-active --quiet proxy-fast-sub &&
        python3 "$LIB/runtime.py" probe "$BASE" 2>/dev/null; then READY=1; break; fi
    sleep 0.25
done
[[ $READY == 1 ]] || die 'Local readiness check failed.'
python3 "$LIB/runtime.py" verify-proxy "$BASE" "$PUBLIC" "$CORE"
log "Ready in $((SECONDS-START)) seconds. Proxy egress and subscription checked; cloud ingress must be tested from your computer."
python3 - "$BASE/result.json" "$((SECONDS-START))" <<'PY'
import json,sys
path=sys.argv[1]; r=json.load(open(path)); r['install_seconds']=int(sys.argv[2]); r['proxy_egress_verified']=True
with open(path,'w') as f: json.dump(r,f,ensure_ascii=False,indent=2); f.write('\n')
print('Mihomo:  '+r['mihomo'])
print('sing-box: '+r['sing_box'])
print('Elapsed:  '+sys.argv[2]+' seconds')
print('ECS security group: allow inbound TCP 443 and TCP 8080. Keep your SSH access.')
print('Subscriptions use HTTP + a random secret path. Result saved: /etc/proxy-fast/result.json')
PY
