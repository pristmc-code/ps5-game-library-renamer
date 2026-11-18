"""Versao de cada jogo e "existe versao mais nova na Sony" (1.3), com o glibren rodando no PS5.

O PS5 nunca fala com a Sony: este app (no PC) pede ao glibren a lista dos jogos (GET http://IP:8765/api/versoes, com a
versao local e o versionJsonUri de cada jogo de PS5), consulta o servidor de atualizacoes da Sony e devolve os
resultados ao glibren (POST /api/versoes), que mostra "Existe X (pede firmware Y)" na lista dele.

Certificado: o servidor sgst.prod.dl.playstation.net usa a raiz propria "SCEI DNAS Root 05" (assinada com SHA-1, valida
ate 2037). O contexto TLS desta consulta confia SO nessa raiz (scei_dnas_root05.pem, conferida pelo SHA-256), confere o
nome do host e aceita SHA-1 (OpenSSL "DEFAULT@SECLEVEL=0") SO nesse contexto, usado SO para esse host. Nunca desliga a
verificacao.

Pareamento: o glibren so aceita o POST de um aparelho pareado (codigo mostrado na TV). O token fica em glibren.json, na
pasta do app (arquivo local, fora do Git, como o INI).
"""

import binascii
import hashlib
import hmac
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import ftp_ps5

HOST_SONY = "sgst.prod.dl.playstation.net"
# PS4 (CUSA): outro servidor, com a MESMA raiz SCEI (certificado *.ww.np.dl.playstation.net). Endereco:
# https://<HOST_PS4>/plo/np/<CUSA>/<hmac_sha256(chave, "np_" + CUSA)>/<CUSA>-ver.xml. Chave publica tirada de
# andshrew/PS4-Updates-Python (MIT, ps4_updates/title.py), que credita o gist de Zer0xFF
# (gist.github.com/Zer0xFF/d94818f15e3e85b0b4d48000a4be1c73); conferida com o exemplo deles (CUSA00897).
HOST_PS4 = "gs-sec.ww.np.dl.playstation.net"
_CHAVE_PS4 = binascii.unhexlify("AD62E37F905E06BC19593142281C112CEC0E7EC3E97EFDCAEFCDBAAFA6378D84")
SHA256_RAIZ = "51D5A7833F67F1D1C6212F75997E4B83DADA0F7910BA4B5A3FA4E9B7B9BC827C"
ARQUIVO_RAIZ = ftp_ps5.PASTA_RECURSOS / "scei_dnas_root05.pem"
ARQUIVO_ESTADO = ftp_ps5.PASTA_APP / "glibren.json"
PORTA_GLIBREN = int(os.environ.get("GLR_TESTE_PORTA_GLIBREN", "8765"))   # variavel so para os testes
PARALELO = 2
TEMPO_SONY = 15
TEMPO_LISTA = 120          # a 1a lista le de dentro de cada imagem no PS5: pode demorar no HD USB
MAX_RESPOSTA = 512 * 1024
RE_ENDERECO = re.compile(r"^https://" + re.escape(HOST_SONY) + r"/[A-Za-z0-9/._-]{1,180}$")
RE_ENDERECO_PS4 = re.compile(r"^https://" + re.escape(HOST_PS4) + r"/plo/np/CUSA\d{5}/[0-9a-f]{64}/CUSA\d{5}-ver\.xml$")
RE_PACOTE = re.compile(r"<package\b([^>]*)>")
RE_ATRIBUTO = re.compile(r'([A-Za-z_][A-Za-z0-9_:-]*)\s*=\s*"([^"]*)"')
RE_IP = re.compile(r"^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}$")


class ErroVersoes(Exception):
    def __init__(self, chave: str, mensagem: str, **params):
        super().__init__(mensagem)
        self.chave = chave
        self.params = params


# ---------------------------------------------------------------- Sony

def endereco_ok(url: str) -> bool:
    return isinstance(url, str) and bool(RE_ENDERECO.match(url) or RE_ENDERECO_PS4.match(url)) and ".." not in url


def endereco_ps4(cusa: str) -> str:
    """Endereco do -ver.xml de um jogo de PS4 ("" se o codigo nao for CUSA00000)."""
    if not isinstance(cusa, str) or not re.fullmatch(r"CUSA\d{5}", cusa):
        return ""
    h = hmac.new(_CHAVE_PS4, f"np_{cusa}".encode(), hashlib.sha256).hexdigest()
    return f"https://{HOST_PS4}/plo/np/{cusa}/{h}/{cusa}-ver.xml"


def contexto_sony() -> ssl.SSLContext:
    """TLS que confia so na raiz SCEI fixada (conferida pelo SHA-256) e confere o nome do host."""
    pem = ARQUIVO_RAIZ.read_text(encoding="ascii")
    der = ssl.PEM_cert_to_DER_cert(pem)
    if hashlib.sha256(der).hexdigest().upper() != SHA256_RAIZ:
        raise ErroVersoes("raiz_invalida", "o certificado da Sony guardado no app nao e o SCEI DNAS Root 05")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)   # check_hostname e CERT_REQUIRED ligados
    ctx.load_verify_locations(cadata=pem)            # so esta raiz (nenhuma do sistema)
    ctx.set_ciphers("DEFAULT@SECLEVEL=0")            # a raiz e assinada com SHA-1: so neste contexto
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def fw_de_system_ver(decimal: str) -> str:
    """system_ver em decimal: em hexa e o firmware em BCD (337641479 = 0x14200007 = "14.20")."""
    try:
        v = int(str(decimal).strip())
    except ValueError:
        return ""
    if v > 0xFFFFFFFF:
        v >>= 32
    maior, menor = (v >> 24) & 0xFF, (v >> 16) & 0xFF
    if not maior or any(n > 9 for n in (maior >> 4, maior & 15, menor >> 4, menor & 15)):
        return ""
    return f"{maior:x}.{menor:02x}"


def comparar_versao(a: str, b: str) -> int:
    """"02.000.003" x "01.000.010" (e firmwares "14.20" x "12.00"): parte por parte, como numero."""
    def partes(s):
        return [int(p) if p.isdigit() else 0 for p in str(s).split(".")]
    x, y = partes(a), partes(b)
    n = max(len(x), len(y))
    x += [0] * (n - len(x))
    y += [0] * (n - len(y))
    return (x > y) - (x < y)


def ler_version_xml(xml: str) -> dict:
    """Le os <package content_ver system_ver size>. Varios (pre-download): o maior fica; o outro em outra_versao."""
    r = {"versao": "", "fw": "", "tamanho": 0, "outra_versao": "", "outro_fw": "", "erro": ""}
    if "<title_patch" not in (xml or "") and "<titlepatch" not in (xml or ""):   # PS5 / PS4
        r["erro"] = "resposta sem title_patch"
        return r
    for m in RE_PACOTE.finditer(xml):
        attrs = dict(RE_ATRIBUTO.findall(m.group(1)))
        versao = attrs.get("content_ver") or attrs.get("version", "")   # PS5 / PS4
        if not re.fullmatch(r"\d{1,4}(\.\d{1,4}){0,3}", versao):
            continue
        fw = fw_de_system_ver(attrs.get("system_ver", ""))
        tamanho = int(attrs["size"]) if attrs.get("size", "").isdigit() else 0
        if not r["versao"] or comparar_versao(versao, r["versao"]) > 0:
            if r["versao"] and r["versao"] != versao:
                r["outra_versao"], r["outro_fw"] = r["versao"], r["fw"]
            r["versao"], r["fw"], r["tamanho"] = versao, fw, tamanho
        elif versao != r["versao"] and (not r["outra_versao"] or comparar_versao(versao, r["outra_versao"]) > 0):
            r["outra_versao"], r["outro_fw"] = versao, fw
    if not r["versao"]:
        r["erro"] = "resposta sem pacote"
    return r


class _SemRedirecionar(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):   # redirecionamento poderia levar a outro host
        return None


def consultar_sony(url: str, ctx: ssl.SSLContext) -> dict:
    """GET do version.xml. Falha de rede/TLS/HTTP vira "erro" (nunca excecao)."""
    if not endereco_ok(url):
        return {**ler_version_xml(""), "erro": "endereco recusado"}
    abridor = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), _SemRedirecionar)
    try:
        with abridor.open(urllib.request.Request(url, method="GET"), timeout=TEMPO_SONY) as resp:
            corpo = resp.read(MAX_RESPOSTA + 1)
            if len(corpo) > MAX_RESPOSTA:
                return {**ler_version_xml(""), "erro": "resposta grande demais"}
            return ler_version_xml(corpo.decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return {**ler_version_xml(""), "erro": "sem atualizacao publicada (HTTP 404)" if e.code == 404 else f"HTTP {e.code}"}
    except ssl.SSLError as e:
        return {**ler_version_xml(""), "erro": f"certificado ou TLS recusado: {str(e)[:100]}"}
    except (urllib.error.URLError, OSError) as e:
        return {**ler_version_xml(""), "erro": f"rede: {str(getattr(e, 'reason', e))[:100]}"}


# ---------------------------------------------------------------- glibren

def _ler_estado() -> dict:
    try:
        d = json.loads(ARQUIVO_ESTADO.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _gravar_estado(d: dict) -> None:
    tmp = ARQUIVO_ESTADO.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    tmp.replace(ARQUIVO_ESTADO)


def _pedir(ip: str, metodo: str, caminho: str, corpo: bytes | None = None, tipo: str = "",
           token: str = "", tempo: float = 10) -> tuple[int, dict]:
    cab = {"X-GLR": "1"}   # o glibren recusa POST sem ele
    if token:
        cab["X-GLR-Token"] = token
    if corpo is not None:
        cab["Content-Type"] = tipo
    req = urllib.request.Request(f"http://{ip}:{PORTA_GLIBREN}{caminho}", data=corpo, method=metodo, headers=cab)
    abridor = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # rede local: sem proxy
    try:
        with abridor.open(req, timeout=tempo) as r:
            texto = r.read(8 << 20).decode("utf-8", "replace")
            return r.status, json.loads(texto or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read(1 << 16).decode("utf-8", "replace") or "{}")
        except ValueError:
            return e.code, {}


class Versoes:
    """Estado da tela "Versoes (Sony)" do app (uma consulta por vez)."""

    def __init__(self):
        self._trava = threading.Lock()
        self.ocupado = False
        self.mensagem = ""
        self.tipo = "info"
        self.feitos = self.total = 0
        self.jogos: list[dict] = []
        self.resultados: dict[str, dict] = {}
        self.fw_console = ""
        self.conectado = False
        self.pareado = False

    # -- auxiliares
    def ip(self) -> str:
        d = _ler_estado()
        if d.get("ip"):
            return d["ip"]
        try:
            return str(ftp_ps5.ler_config().get("host", ""))
        except Exception:
            return ""

    def _token(self) -> str:
        return str(_ler_estado().get("token", ""))

    def _avisar(self, chave: str, tipo: str = "info", **params) -> None:
        self.mensagem = {"chave": chave, "params": params}
        self.tipo = tipo

    def estado(self) -> dict:
        jogos = []
        for j in self.jogos:
            x = {k: v for k, v in j.items() if k != "versionJsonUri"}
            x["ps5"] = "versionJsonUri" in j or bool(endereco_ps4(j.get("titleId", "")))   # = consultavel
            if j.get("titleId") in self.resultados:
                x["sony"] = self.resultados[j["titleId"]]
            jogos.append(x)
        return {"ok": True, "ip": self.ip(), "ocupado": self.ocupado, "mensagem": self.mensagem, "tipo": self.tipo,
                "feitos": self.feitos, "total": self.total, "fw": self.fw_console, "conectado": self.conectado,
                "pareado": self.pareado, "jogos": jogos}

    def _rodar(self, fn, *args) -> None:
        with self._trava:
            if self.ocupado:
                raise ErroVersoes("ver_ocupado", "uma acao ja esta em andamento")
            self.ocupado = True

        def alvo():
            try:
                fn(*args)
            except Exception as e:   # qualquer falha vira mensagem na tela
                self._avisar("ver_falha", "bad", e=str(e)[:200])
            finally:
                self.ocupado = False
        threading.Thread(target=alvo, daemon=True).start()

    # -- acoes
    def conectar(self, ip: str) -> None:
        ip = str(ip).strip()
        if not RE_IP.match(ip):
            raise ErroVersoes("ver_ip_invalido", "IP invalido")
        d = _ler_estado()
        if d.get("ip") != ip:
            d["ip"] = ip
            _gravar_estado(d)
        self._rodar(self._carregar)

    def _carregar(self) -> bool:
        ip = self.ip()
        self._avisar("ver_lendo")
        try:
            status, d = _pedir(ip, "GET", "/api/versoes", tempo=TEMPO_LISTA)
        except (OSError, ValueError):
            self.conectado = False
            self._avisar("ver_sem_resposta", "bad", ip=ip)
            return False
        if status == 404:
            self.conectado = False
            self._avisar("ver_glibren_antigo", "bad")
            return False
        if status != 200 or not d.get("ok"):
            self.conectado = False
            self._avisar("ver_sem_resposta", "bad", ip=ip)
            return False
        self.jogos = [j for j in d.get("jogos", []) if isinstance(j, dict)]
        self.fw_console = str(d.get("fw_console", ""))
        self.resultados = {r.get("titleId"): r for r in d.get("resultados", []) if isinstance(r, dict)}
        self.conectado = True
        try:
            _, p = _pedir(ip, "GET", "/api/pareamento", token=self._token(), tempo=5)
            self.pareado = bool(p.get("pareado")) or not p.get("ligado", True)
        except (OSError, ValueError):
            self.pareado = False
        ps5 = sum(1 for j in self.jogos if "versionJsonUri" in j or endereco_ps4(j.get("titleId", "")))
        self._avisar("ver_lista", "ok", n=len(self.jogos), ps5=ps5)
        return True

    def pedir_codigo(self) -> None:
        self._rodar(self._pedir_codigo)

    def _pedir_codigo(self) -> None:
        status, d = _pedir(self.ip(), "POST", "/api/parear/pedir", b"", "application/x-www-form-urlencoded", tempo=8)
        if d.get("ok"):
            self._avisar("ver_codigo_tv", "ok")
        elif "espera" in d:
            self._avisar("ver_espere", "bad", s=d.get("espera"))
        else:
            self._avisar("ver_recusou", "bad", e=d.get("erro", f"HTTP {status}"))

    def parear(self, codigo: str) -> None:
        codigo = str(codigo).strip()
        if not re.fullmatch(r"\d{6}", codigo):
            raise ErroVersoes("ver_codigo_invalido", "o codigo tem 6 numeros")
        self._rodar(self._parear, codigo)

    def _parear(self, codigo: str) -> None:
        corpo = urllib.parse.urlencode({"codigo": codigo, "nome": "Game Library Renamer PC"}).encode()
        status, d = _pedir(self.ip(), "POST", "/api/parear", corpo, "application/x-www-form-urlencoded", tempo=8)
        token = str(d.get("token", ""))
        if d.get("ok") and re.fullmatch(r"[0-9a-f]{64}", token):
            e = _ler_estado()
            e["token"] = token
            _gravar_estado(e)
            self.pareado = True
            self._avisar("ver_pareado", "ok")
            return
        chave = d.get("chave", "")
        if chave == "parear_codigo_errado":
            self._avisar("ver_codigo_errado", "bad", n=d.get("restam", 0))
        elif chave in ("parear_expirou", "parear_sem_codigo"):
            self._avisar("ver_codigo_expirou", "bad")
        elif chave == "parear_bloqueado":
            self._avisar("ver_bloqueado", "bad", s=d.get("espera", 0))
        else:
            self._avisar("ver_recusou", "bad", e=d.get("erro", f"HTTP {status}"))

    def consultar(self) -> None:
        self._rodar(self._consultar)

    def _consultar(self) -> None:
        ctx = contexto_sony()
        if not self._carregar():
            return
        if not self.pareado:
            self._avisar("ver_parear_antes", "bad")
            return
        alvos: dict[str, str] = {}
        for j in self.jogos:   # um pedido por jogo (o mesmo jogo em duas pastas tem o mesmo endereco)
            uri = j.get("versionJsonUri", "") or endereco_ps4(j.get("titleId", ""))   # PS4: sai do Title ID
            if endereco_ok(uri):
                alvos.setdefault(j.get("titleId", ""), uri)
        if not alvos:
            self._avisar("ver_nenhum_ps5")
            return
        self.total, self.feitos = len(alvos), 0
        self._avisar("ver_consultando", a=0, b=self.total)
        novos, trava = [], threading.Lock()

        def um(tid, uri):
            r = consultar_sony(uri, ctx)
            x = {"titleId": tid, "consultado_em": int(time.time())}
            if r["versao"]:
                x["versao_nova"] = r["versao"]
                if r["fw"]:
                    x["fw_exigido"] = r["fw"]
                if r["tamanho"]:
                    x["tamanho"] = r["tamanho"]
                if r["outra_versao"]:
                    x["outra_versao"] = r["outra_versao"]
                    if r["outro_fw"]:
                        x["outro_fw"] = r["outro_fw"]
            if r["erro"]:
                x["erro"] = r["erro"]
            with trava:
                novos.append(x)
                self.resultados[tid] = x
                self.feitos += 1
                self._avisar("ver_consultando", a=self.feitos, b=self.total)

        with ThreadPoolExecutor(PARALELO) as pool:
            list(pool.map(lambda a: um(*a), alvos.items()))
        corpo = json.dumps({"resultados": novos}).encode()
        status, d = _pedir(self.ip(), "POST", "/api/versoes", corpo, "application/json", self._token(), tempo=20)
        if status == 401:
            e = _ler_estado()
            e.pop("token", None)
            _gravar_estado(e)
            self.pareado = False
            self._avisar("ver_esqueceu", "bad")
            return
        if not d.get("ok"):
            self._avisar("ver_nao_guardou", "bad", e=d.get("erro", f"HTTP {status}"))
            return
        novas = sum(1 for j in self.jogos if (s := self.resultados.get(j.get("titleId"))) and s.get("versao_nova")
                    and j.get("versao_local") and comparar_versao(s["versao_nova"], j["versao_local"]) > 0)
        erros = sum(1 for x in novos if x.get("erro"))
        self._avisar("ver_pronto", "ok", n=self.total, novas=novas, erros=erros)
