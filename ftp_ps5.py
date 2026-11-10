"""Acesso ao FTP/SFTP do PS5 e ao arquivo de configuracao GameLibraryRenamer.ini.

- Protocolos: FTP (ftplib) e SFTP (paramiko).
- Lista SOMENTE a raiz das pastas indicadas no INI (nunca entra em subpastas).
- Renomeia arquivos/pastas na mesma pasta, sem nunca sobrescrever um item existente.
"""

import configparser
import ftplib
import posixpath
import re
import stat
import sys
from pathlib import Path

VERSAO = "1.1"

# Empacotado (PyInstaller): dados (INI, cache, base, icones) ao lado do .exe e recursos (HTML)
# na pasta interna do pacote. Rodando como script: tudo na pasta do script.
if getattr(sys, "frozen", False):
    PASTA_APP = Path(sys.executable).resolve().parent
    PASTA_RECURSOS = Path(getattr(sys, "_MEIPASS", PASTA_APP))
else:
    PASTA_APP = PASTA_RECURSOS = Path(__file__).resolve().parent
ARQUIVO_INI = PASTA_APP / "GameLibraryRenamer.ini"
IDIOMAS = ("pt-BR", "en", "es")


class ErroApp(ValueError):
    """Erro com chave de traducao: a tela mostra a mensagem no idioma escolhido."""

    def __init__(self, chave: str, mensagem: str, **params):
        super().__init__(mensagem)
        self.chave = chave
        self.params = params

TIMEOUT = 20
PROTOCOLOS = ("ftp", "sftp")
PORTA_PADRAO = {"ftp": 2121, "sftp": 22}
RE_CODIGO_NOME = re.compile(r"(?<![A-Za-z])([A-Za-z]{4})[-_ ]?(\d{5})(?!\d)")
ERROS_CONEXAO = (OSError, EOFError, *ftplib.all_errors)

CABECALHO_INI = """\
; ============================================================
;  Game Library Renamer - settings
; ============================================================
; [app]     -> language = pt-BR, en or es (saved by the language selector).
; [ps5]     -> PS5 connection, saved by the "Save" button in the app.
;              protocol = ftp or sftp
;              Default = "ftpsrv" payload (ps5-payload-dev), the most used FTP on the PS5:
;                protocol = ftp, port = 2121, no user and no password (any login is accepted).
;              With the etaHEN FTP, its default port is 1337 (also no password).
;              WARNING: the password is stored as plain text in this file.
; [folders] -> folders read when you click "Connect".
;              One per line, as  name = /full/path
;              Only the root of each folder is read (subfolders are NOT opened).
; Lines starting with ; are comments.
; ============================================================

"""

PASTAS_EXEMPLO = {"folder1": "/data/homebrew/", "folder2": "/mnt/ext1/homebrew/"}

# Nomes em ingles (atuais) e em portugues (INIs gravados antes): le os dois, grava sempre em ingles.
SECAO_PASTAS = ("folders", "pastas")
CHAVES = {"protocolo": ("protocol", "protocolo"), "host": ("host",), "porta": ("port", "porta"),
          "usuario": ("user", "usuario"), "senha": ("password", "senha")}
CHAVE_IDIOMA = ("language", "idioma")


# ----------------------------------------------------------------------------- INI

def _novo_parser() -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser()  # Raw: senha com % nao quebra a leitura
    parser.optionxform = str
    return parser


def _ler_ini() -> configparser.RawConfigParser:
    """Le o INI aceitando "UTF-8 com BOM"; erro de sintaxe vira mensagem para a tela (nao derruba o app)."""
    parser = _novo_parser()
    if ARQUIVO_INI.exists():
        try:
            parser.read(ARQUIVO_INI, encoding="utf-8-sig")
        except (configparser.Error, UnicodeDecodeError) as erro:
            raise ErroApp("ini_invalido", f"{ARQUIVO_INI.name} com erro: {erro}",
                          ini=str(ARQUIVO_INI), detalhe=" ".join(str(erro).split())[:200]) from erro
    return parser


def _valor(parser, secao: str, nomes: tuple, padrao: str = "") -> str:
    for nome in nomes:
        if parser.has_option(secao, nome):
            return parser.get(secao, nome)
    return padrao


def _pastas_do_ini(parser) -> dict:
    secao = next((s for s in SECAO_PASTAS if parser.has_section(s)), None)
    return dict(parser[secao]) if secao else {}


def _conexao_do_ini(parser) -> dict:
    return {interna: _valor(parser, "ps5", nomes) for interna, nomes in CHAVES.items()}


def _conexao_modelo() -> dict:
    return {"protocolo": "ftp", "host": "", "porta": 2121, "usuario": "", "senha": ""}


def ler_config() -> dict:
    """Le o INI (cria um modelo se nao existir) e devolve conexao + pastas + idioma."""
    if not ARQUIVO_INI.exists():
        gravar_config(_conexao_modelo(), PASTAS_EXEMPLO, idioma="")
    parser = _ler_ini()
    if not parser.has_section("ps5") and not any(parser.has_section(s) for s in SECAO_PASTAS):
        # INI so com o idioma (criado pelo instalador): completa com o modelo, mantendo o idioma.
        gravar_config(_conexao_modelo(), PASTAS_EXEMPLO)
        parser = _ler_ini()
    elif parser.has_section("pastas") or _valor(parser, "ps5", ("protocolo",)) or _valor(parser, "app", ("idioma",)):
        # INI com os nomes antigos em portugues: regrava com os nomes em ingles (mesmos valores).
        gravar_config()
        parser = _ler_ini()
    ps5 = _conexao_do_ini(parser)
    idioma = _valor(parser, "app", CHAVE_IDIOMA).strip()
    pastas = [v.strip() for v in _pastas_do_ini(parser).values() if v.strip()]
    protocolo = ps5["protocolo"].strip().lower()
    protocolo = protocolo if protocolo in PROTOCOLOS else "ftp"
    porta = ps5["porta"].strip()
    return {
        "protocolo": protocolo,
        "host": ps5["host"].strip(),
        "porta": int(porta) if porta.isdigit() else PORTA_PADRAO[protocolo],
        "usuario": ps5["usuario"].strip(),
        "senha": ps5["senha"],
        "pastas": pastas,
        "idioma": idioma if idioma in IDIOMAS else "",
        "arquivo_ini": str(ARQUIVO_INI),
        "versao": VERSAO,
    }


def gravar_config(conexao: dict | None = None, pastas: dict | None = None, idioma: str | None = None) -> None:
    """Grava o INI (sempre com nomes em ingles). O que vier None e mantido como esta no arquivo."""
    parser = _ler_ini()
    if pastas is None:
        pastas = _pastas_do_ini(parser)
    if conexao is None:
        conexao = _conexao_do_ini(parser)
    if idioma is None:
        idioma = _valor(parser, "app", CHAVE_IDIOMA)
    protocolo = conexao.get("protocolo", "ftp") or "ftp"
    linhas = [CABECALHO_INI, "[app]", f"language = {idioma}", "", "[ps5]",
              f"protocol = {protocolo}",
              f"host = {str(conexao.get('host', '')).strip()}",
              f"port = {int(conexao.get('porta') or PORTA_PADRAO.get(protocolo, 2121))}",
              f"user = {str(conexao.get('usuario', '')).strip()}",
              f"password = {conexao.get('senha', '')}",
              "", "[folders]"]
    # Renumera como folder1, folder2... (tambem converte as chaves antigas pasta1, pasta2...).
    valores = [str(v).strip() for v in pastas.values() if str(v).strip()]
    linhas += [f"folder{i} = {valor}" for i, valor in enumerate(valores, 1)]
    ARQUIVO_INI.write_text("\n".join(linhas) + "\n", encoding="utf-8")


def normalizar_conexao(dados: dict) -> dict:
    """Aceita host puro ou URL (ftp://host:porta, sftp://host:porta) e completa protocolo/porta."""
    host = str(dados.get("host", "")).strip()
    protocolo = str(dados.get("protocolo", "") or "ftp").strip().lower()
    porta = str(dados.get("porta", "")).strip()
    url = re.match(r"^(s?ftp)://([^/:]+)(?::(\d+))?/?$", host, re.I)
    if url:
        protocolo, host = url.group(1).lower(), url.group(2)
        porta = url.group(3) or porta
    if protocolo not in PROTOCOLOS:
        raise ErroApp("protocolo_invalido", "protocolo deve ser ftp ou sftp")
    if not host:
        raise ErroApp("informe_ip", "informe o IP/endereco do PS5")
    return {"protocolo": protocolo, "host": host,
            "porta": int(porta) if porta.isdigit() else PORTA_PADRAO[protocolo],
            "usuario": str(dados.get("usuario", "")), "senha": str(dados.get("senha", ""))}


# ----------------------------------------------------------------------------- clientes

class ClienteFtp:
    def __init__(self, conexao: dict):
        self.ftp = ftplib.FTP(timeout=TIMEOUT, encoding="utf-8")
        self.ftp.connect(conexao["host"], conexao["porta"])
        usuario = conexao["usuario"].strip()
        if usuario:
            self.ftp.login(usuario, conexao["senha"])
        else:
            self.ftp.login()  # anonimo
        self.ftp.set_pasv(True)

    def existe_pasta(self, pasta: str) -> bool:
        # CWD confirma a existencia: alguns servidores devolvem LIST vazio para pasta inexistente.
        try:
            self.ftp.cwd(pasta)
            return True
        except ftplib.error_perm:
            return False

    def listar(self, pasta: str) -> list[dict]:
        """Lista so a raiz da pasta. Tenta MLSD; se o servidor nao suportar, usa LIST."""
        itens = []
        # Entra na pasta e pede o MLSD sem caminho e sem OPTS: o ftpsrv do PS5 ignora o caminho do
        # MLSD (lista sempre a pasta atual) e nao conhece OPTS MLST.
        self.ftp.cwd(pasta)
        try:
            for nome, fatos in self.ftp.mlsd():
                tipo = fatos.get("type", "")
                if nome in (".", "..") or tipo in ("cdir", "pdir"):
                    continue
                tamanho = fatos.get("size", "")
                itens.append({"nome": nome, "tipo": "pasta" if tipo == "dir" else "arquivo",
                              "tamanho": int(tamanho) if tamanho.isdigit() else None})
            return itens
        except ftplib.error_perm:
            itens = []

        linhas: list[str] = []
        self.ftp.retrlines(f"LIST {pasta}", linhas.append)
        for linha in linhas:
            partes = linha.split(None, 8)
            if len(partes) < 9 or linha.startswith("total"):
                continue
            nome = partes[8]
            if linha.startswith("l") and " -> " in nome:
                nome = nome.split(" -> ", 1)[0]
            if nome in (".", ".."):
                continue
            itens.append({"nome": nome, "tipo": "pasta" if linha.startswith("d") else "arquivo",
                          "tamanho": int(partes[4]) if partes[4].isdigit() else None})
        return itens

    def renomear(self, origem: str, destino: str) -> None:
        self.ftp.rename(origem, destino)

    def fechar(self) -> None:
        try:
            self.ftp.quit()
        except ftplib.all_errors:
            self.ftp.close()


class ClienteSftp:
    def __init__(self, conexao: dict):
        import paramiko  # importado aqui: so e exigido quando o protocolo e SFTP

        self.transporte = paramiko.Transport((conexao["host"], conexao["porta"]))
        self.transporte.banner_timeout = TIMEOUT
        try:
            self.transporte.connect(username=conexao["usuario"].strip(), password=conexao["senha"])
            self.sftp = paramiko.SFTPClient.from_transport(self.transporte)
            self.sftp.get_channel().settimeout(TIMEOUT)
        except Exception as erro:
            self.transporte.close()
            raise ErroApp("falha_sftp", f"falha no SFTP: {erro}", detalhe=str(erro)) from erro

    def existe_pasta(self, pasta: str) -> bool:
        try:
            return stat.S_ISDIR(self.sftp.stat(pasta).st_mode)
        except FileNotFoundError:
            return False

    def listar(self, pasta: str) -> list[dict]:
        return [{"nome": a.filename, "tipo": "pasta" if stat.S_ISDIR(a.st_mode or 0) else "arquivo",
                 "tamanho": a.st_size}
                for a in self.sftp.listdir_attr(pasta) if a.filename not in (".", "..")]

    def renomear(self, origem: str, destino: str) -> None:
        self.sftp.rename(origem, destino)

    def fechar(self) -> None:
        self.transporte.close()


def conectar(conexao: dict):
    return ClienteSftp(conexao) if conexao["protocolo"] == "sftp" else ClienteFtp(conexao)


# ----------------------------------------------------------------------------- operacoes

# Arquivos compactados nao sao jogos instalaveis: ficam fora da lista (nem aparecem nem sao renomeados).
EXTENSOES_COMPACTADAS = (".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".lz", ".lzma",
                         ".zst", ".cab", ".arj", ".lzh", ".z")
RE_PARTE_COMPACTADA = re.compile(r"\.(r\d{2}|z\d{2}|\d{3})$", re.IGNORECASE)  # volumes: .r00, .z01, .001


# Patches e backports levam o Title ID do jogo no nome: se fossem renomeados, ficariam com o nome do jogo.
RE_PATCH = re.compile(r"patch|pacth|backport|back[-_ .]port|update", re.IGNORECASE)


def eh_patch(nome: str) -> bool:
    return bool(RE_PATCH.search(nome))


# Downloads em andamento: lojas, navegadores e clientes torrent gravam o arquivo com uma terminacao
# temporaria (ex.: BlackBox "jogo.exfat.part") e so tiram no fim. Renomear no meio quebraria o download.
SUFIXOS_DOWNLOAD = (".part", ".partial", ".crdownload", ".download", ".opdownload", ".fdmdownload",
                    ".tmp", ".temp", ".incomplete", ".filepart", ".dtapart", ".!qb", ".!ut", ".!bt",
                    ".bc!", ".az!", ".!sync", ".xltd", ".td", ".aria2")
RE_DOWNLOAD = re.compile(r"\.part\d+$|^\.syncthing\.|^~", re.IGNORECASE)  # .part1 (JDownloader), Syncthing


def em_download(nome: str, nomes_da_pasta: set[str] = frozenset()) -> bool:
    """Nome temporario de download, ou arquivo com o controle do downloader ao lado (aria2: jogo.exfat
    + jogo.exfat.aria2; outros: jogo.exfat + jogo.exfat.part), que grava direto no nome final."""
    baixo = nome.lower()
    if baixo.endswith(SUFIXOS_DOWNLOAD) or RE_DOWNLOAD.search(nome):
        return True
    return any(nome + sufixo in nomes_da_pasta for sufixo in (".aria2", ".part", ".partial", ".tmp"))


def compactado(item: dict) -> bool:
    nome = item["nome"].lower()
    return item["tipo"] == "arquivo" and (nome.endswith(EXTENSOES_COMPACTADAS) or bool(RE_PARTE_COMPACTADA.search(nome)))


def extrair_codigo(nome: str) -> str:
    achado = RE_CODIGO_NOME.search(nome)
    return (achado.group(1) + achado.group(2)).upper() if achado else ""


def escanear(conexao: dict, pastas: list[str]) -> dict:
    """Conecta, testa se cada pasta existe e devolve os itens da raiz com o codigo extraido do nome."""
    itens, situacao = [], []
    cliente = conectar(conexao)
    try:
        for pasta in pastas:
            estado = {"pasta": pasta, "existe": False, "qtd": 0, "erro": ""}
            situacao.append(estado)
            try:
                estado["existe"] = cliente.existe_pasta(pasta)
                if not estado["existe"]:
                    continue
                listagem = cliente.listar(pasta)
                nomes = {item["nome"] for item in listagem}
                for item in listagem:
                    # So arquivos: pastas e compactados ficam fora da lista.
                    if item["tipo"] != "arquivo" or compactado(item):
                        continue
                    item.update(pasta=pasta, codigo=extrair_codigo(item["nome"]), patch=eh_patch(item["nome"]),
                                baixando=em_download(item["nome"], nomes))
                    itens.append(item)
                    estado["qtd"] += 1
            except ERROS_CONEXAO as erro:
                estado["erro"] = str(erro).strip() or erro.__class__.__name__
    finally:
        cliente.fechar()
    return {"itens": itens, "situacao": situacao}


def _nome_seguro(nome: str) -> bool:
    return bool(nome) and nome not in (".", "..") and "/" not in nome and "\\" not in nome


def renomear(conexao: dict, pasta: str, de: str, para: str, tamanho: int | None = None) -> None:
    """Renomeia pasta/de -> pasta/para (mesma pasta, sem mover entre diretorios).
    tamanho: o da busca; se mudou desde entao, o arquivo ainda esta sendo gravado e nao e renomeado."""
    if not _nome_seguro(de) or not _nome_seguro(para):
        raise ErroApp("nome_invalido", "nome invalido")
    cliente = conectar(conexao)
    try:
        # Alguns servidores sobrescrevem o destino no rename: confere antes para nunca apagar nada.
        listagem = cliente.listar(pasta)
        existentes = {item["nome"] for item in listagem}
        if de not in existentes:
            raise ErroApp("origem_sumiu", f"'{de}' nao existe mais na pasta", nome=de)
        atual = next(item.get("tamanho") for item in listagem if item["nome"] == de)
        if em_download(de, existentes) or (tamanho is not None and atual is not None and atual != tamanho):
            raise ErroApp("em_download", f"'{de}' ainda esta sendo baixado", nome=de)
        if para in existentes:
            raise ErroApp("destino_existe", f"ja existe '{para}' na pasta", nome=para)
        cliente.renomear(posixpath.join(pasta, de), posixpath.join(pasta, para))
    finally:
        cliente.fechar()
