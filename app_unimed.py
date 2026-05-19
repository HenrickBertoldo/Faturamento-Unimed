import hashlib
import streamlit as st
import xml.etree.ElementTree as ET
import pandas as pd
import io
from datetime import datetime, timedelta
from streamlit_gsheets import GSheetsConnection

st.set_page_config(
    page_title="Faturamento TISS Cloud - Unimed",
    layout="wide",
    page_icon="🛠️"
)

# ==========================================
# CONSTANTES
# ==========================================
NS = {'ans': 'http://www.ans.gov.br/padroes/tiss/schemas'}
ET.register_namespace('ans', 'http://www.ans.gov.br/padroes/tiss/schemas')
ET.register_namespace('xsi', 'http://www.w3.org/2001/XMLSchema-instance')

# MELHORIA 1: Constantes nomeadas em vez de strings mágicas espalhadas pelo código
CODIGOS_OXIGENIO = {'60034335', '60034343'}
UNIDADE_OXIGENIO_HORA = '60034335'
UNIDADE_OXIGENIO_MIN  = '60034343'
PREFIXO_UBERLANDIA    = '0014'

SEQUENCIA_PROC_TISS = [
    'sequencialItem', 'dataExecucao', 'horaInicial', 'horaFinal',
    'procedimento', 'quantidadeExecutada', 'viaAcesso', 'tecnicaUtilizada',
    'reducaoAcrescimo', 'valorUnitario', 'valorTotal', 'faturamentoCumulativo',
]
SEQUENCIA_SERVICO_TISS = [
    'dataExecucao', 'horaInicial', 'horaFinal', 'codigoTabela', 'codigoProcedimento',
    'quantidadeExecutada', 'unidadeMedida', 'reducaoAcrescimo', 'valorUnitario',
    'valorTotal', 'descricaoProcedimento', 'registroANVISA', 'codigoRefFabricante',
]

ABAS = [
    'medicos', 'procedimentos', 'conveniados',
    'blindagem', 'itens', 'unidades', 'anvisa'
]

TABELAS_PADRAO = {
    'medicos':       pd.DataFrame(columns=['Nome do Médico', 'CBO Correto', 'Substituir por Cód. Operadora', 'Código na Operadora']),
    'procedimentos': pd.DataFrame(columns=['Código do Procedimento', 'Grau Part Obrigatório', 'Via de Acesso (1, 2 ou EXCLUIR)', 'Técnica (1, 2 ou EXCLUIR)']),
    'conveniados':   pd.DataFrame(columns=['Nome do Médico Conveniado']),
    'blindagem':     pd.DataFrame(columns=['Código Prestador Protegido']),
    'itens':         pd.DataFrame(columns=['Código Incorreto', 'Código Correto']),
    'unidades':      pd.DataFrame(columns=['Código do Item', 'Unidade de Medida Correta']),
    'anvisa':        pd.DataFrame(columns=['Código do Item', 'Registro ANVISA', 'Ref. Fabricante']),
}

CONFIG_COLUNAS_TEXTO = {
    col: st.column_config.TextColumn(col, required=True)
    for col in [
        'Código do Item', 'Código Incorreto', 'Código Correto',
        'Código do Procedimento', 'Código Prestador Protegido',
        'Unidade de Medida Correta', 'Registro ANVISA', 'Ref. Fabricante',
    ]
}

# ==========================================
# UTILITÁRIOS
# ==========================================
def ans_tag(tag_name: str) -> str:
    return f"{{{NS['ans']}}}{tag_name}"

def tag_limpa(element) -> str:
    return element.tag.split('}')[-1] if '}' in element.tag else element.tag

def limpar_numero(valor) -> str:
    v = str(valor).strip()
    if v.lower() in ('nan', 'none', '<na>', ''):
        return ''
    return v.removesuffix('.00').removesuffix('.0')

def padronizar_8_digitos(cod: str) -> str:
    """Garante 8 dígitos adicionando zero à esquerda quando necessário."""
    c = limpar_numero(cod)
    return ('0' + c) if (len(c) == 7 and c.isdigit()) else c

def normalizar_texto(t: str) -> str:
    """Uppercase + strip para comparações de nome."""
    return str(t).strip().upper()

# ==========================================
# BANCO DE DADOS — GOOGLE SHEETS
# ==========================================

# MELHORIA 2: Função genérica de leitura, elimina duplicação entre as duas funções
def _ler_sheets(conn) -> dict:
    dados = {}
    for aba in ABAS:
        try:
            df = conn.read(worksheet=aba, ttl=0, dtype=str)
            if df is not None and not df.empty:
                for col in df.columns:
                    df[col] = df[col].astype(str).apply(limpar_numero)
                dados[aba] = df
            else:
                dados[aba] = TABELAS_PADRAO[aba].copy()
        except Exception:
            dados[aba] = TABELAS_PADRAO[aba].copy()
    return dados

def carregar_sheets(silencioso: bool = True):
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        dados = _ler_sheets(conn)
        for aba, df in dados.items():
            st.session_state[f'tab_{aba}'] = df
        if not silencioso:
            st.success("✅ Regras sincronizadas da nuvem com sucesso!")
    except Exception as e:
        for aba in ABAS:
            if f'tab_{aba}' not in st.session_state:
                st.session_state[f'tab_{aba}'] = TABELAS_PADRAO[aba].copy()
        if not silencioso:
            st.error(f"Erro ao conectar com o Google Sheets: {e}")

def salvar_sheets():
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        for aba in ABAS:
            df = st.session_state[f'tab_{aba}'].copy()
            if not df.empty:
                for col in df.columns:
                    df[col] = df[col].astype(str).apply(limpar_numero)
                conn.update(worksheet=aba, data=df)
        st.success("☁️ Alterações gravadas na nuvem com sucesso!")
    except Exception as e:
        st.error(f"Erro ao salvar na nuvem: {e}")

# Inicialização única
if "app_inicializado" not in st.session_state:
    with st.spinner("Conectando à nuvem e sincronizando regras..."):
        carregar_sheets(silencioso=True)
    st.session_state["app_inicializado"] = True

# ==========================================
# MOTOR DE CORREÇÃO — FUNÇÕES AUXILIARES
# ==========================================

def calcular_hora_fim_oxigenio(hora_ini_str: str, qtd: str, cod: str) -> str:
    """Calcula horaFinal para oxigênio com base na quantidade e unidade."""
    try:
        t_ini = datetime.strptime(hora_ini_str.strip(), "%H:%M:%S")
        qtd_f = float(qtd.strip())
        delta = timedelta(hours=qtd_f) if cod == UNIDADE_OXIGENIO_HORA else timedelta(minutes=qtd_f)
        return (t_ini + delta).strftime("%H:%M:%S")
    except Exception:
        return hora_ini_str

def reordenar_proc_executado(proc_elem, via_acao: str, tec_acao: str):
    """Reorganiza filhos de procedimentoExecutado seguindo a sequência TISS."""
    filhos   = {tag_limpa(c): c for c in proc_elem if tag_limpa(c) not in ('viaAcesso', 'tecnicaUtilizada', 'identEquipe')}
    equipes  = [c for c in proc_elem if tag_limpa(c) == 'identEquipe']
    proc_elem.clear()

    for tag in SEQUENCIA_PROC_TISS:
        if tag == 'viaAcesso' and via_acao and via_acao.upper() != 'EXCLUIR':
            ET.SubElement(proc_elem, ans_tag('viaAcesso')).text = via_acao
        elif tag == 'tecnicaUtilizada' and tec_acao and tec_acao.upper() != 'EXCLUIR':
            ET.SubElement(proc_elem, ans_tag('tecnicaUtilizada')).text = tec_acao
        elif tag in filhos:
            proc_elem.append(filhos[tag])

    for eq in equipes:
        proc_elem.append(eq)

def reordenar_servico_executado(servicos_node, nova_anvisa: str = None, nova_ref: str = None):
    """Reorganiza filhos de servicosExecutados e injeta ANVISA/Fabricante."""
    filhos = {tag_limpa(c): c for c in servicos_node}
    servicos_node.clear()

    for tag in SEQUENCIA_SERVICO_TISS:
        if tag == 'registroANVISA' and nova_anvisa:
            ET.SubElement(servicos_node, ans_tag('registroANVISA')).text = nova_anvisa
        elif tag == 'codigoRefFabricante' and nova_ref:
            ET.SubElement(servicos_node, ans_tag('codigoRefFabricante')).text = nova_ref
        elif tag in filhos:
            servicos_node.append(filhos[tag])

# MELHORIA 3: Dicionários de regras construídos uma única vez, fora do loop de guias
def construir_dicionarios(dfs: dict) -> dict:
    return {
        'medicos':    {normalizar_texto(r['Nome do Médico']): r for _, r in dfs['medicos'].iterrows()},
        'procs':      {padronizar_8_digitos(r['Código do Procedimento']): r for _, r in dfs['procedimentos'].iterrows()},
        'conveniados':set(normalizar_texto(x) for x in dfs['conveniados']['Nome do Médico Conveniado'] if pd.notna(x)),
        'blindagem':  set(limpar_numero(x) for x in dfs['blindagem']['Código Prestador Protegido'] if pd.notna(x)),
        'itens':      {padronizar_8_digitos(k): padronizar_8_digitos(v)
                       for k, v in zip(dfs['itens']['Código Incorreto'], dfs['itens']['Código Correto']) if pd.notna(k)},
        'unidades':   {padronizar_8_digitos(r['Código do Item']): limpar_numero(r['Unidade de Medida Correta'])
                       for _, r in dfs['unidades'].iterrows() if pd.notna(r['Código do Item'])},
        'anvisa':     {padronizar_8_digitos(r['Código do Item']): r
                       for _, r in dfs['anvisa'].iterrows() if pd.notna(r['Código do Item'])},
    }

# ==========================================
# MOTOR DE CORREÇÃO — NÚCLEO
# ==========================================

# MELHORIA 4: Funções separadas por responsabilidade (equipe, despesas, oxigênio)
# Cada bloco abaixo tem uma única responsabilidade, facilitando manutenção e testes.

def _processar_equipe(eq, cod_proc: str, is_uberlandia: bool, dicts: dict) -> bool:
    """
    Aplica correções na identEquipe.
    Retorna True se a equipe deve ser REMOVIDA.
    """
    ident = eq.find('ans:identificacaoEquipe', NS)
    if ident is None:
        return False

    nome_elem  = ident.find('ans:nomeProf', NS)
    cbo_elem   = ident.find('ans:CBOS', NS)
    grau_elem  = ident.find('ans:grauPart', NS)
    cod_cont   = ident.find('ans:codProfissional', NS)
    nome_prof  = normalizar_texto(nome_elem.text) if nome_elem is not None and nome_elem.text else ''

    # Corrige CBO e código na operadora pelo cadastro de médicos
    if nome_prof in dicts['medicos']:
        regra = dicts['medicos'][nome_prof]
        cbo_novo = limpar_numero(regra['CBO Correto'])
        if cbo_elem is not None and cbo_novo:
            cbo_elem.text = cbo_novo

        substituir = str(regra['Substituir por Cód. Operadora']).strip().upper()
        if substituir in ('SIM', 'S', 'TRUE') and cod_cont is not None:
            cod_cont.clear()
            ET.SubElement(cod_cont, ans_tag('codigoPrestadorNaOperadora')).text = limpar_numero(regra['Código na Operadora'])

    # Corrige grau de participação pelo cadastro de procedimentos
    if cod_proc in dicts['procs'] and grau_elem is not None:
        grau_novo = limpar_numero(dicts['procs'][cod_proc]['Grau Part Obrigatório'])
        if grau_novo:
            grau_elem.text = grau_novo.zfill(2)

    # Verifica se deve remover médico conveniado (Uberlândia, proc clínico/cirúrgico)
    if is_uberlandia and (cod_proc.startswith('1') or cod_proc.startswith('3')):
        cod_prest_elem = ident.find('.//ans:codigoPrestadorNaOperadora', NS)
        cod_prest = (cod_prest_elem.text or '').strip() if cod_prest_elem is not None else ''
        if cod_prest in dicts['blindagem']:
            return False   # protegido, mantém
        if nome_prof in dicts['conveniados']:
            return True    # remove

    return False


def _processar_procedimentos(procs_container, is_uberlandia: bool, dicts: dict, log: list):
    """Itera sobre procedimentoExecutado aplicando todas as regras."""
    para_remover = []

    for proc in procs_container.findall('ans:procedimentoExecutado', NS):
        cod_elem = proc.find('.//ans:procedimento/ans:codigoProcedimento', NS)
        if cod_elem is None or not cod_elem.text:
            continue

        cod = padronizar_8_digitos(cod_elem.text)

        # De-para de códigos
        if cod in dicts['itens']:
            cod_elem.text = dicts['itens'][cod]
            log.append(f"[DE-PARA] Código procedimento {cod} → {cod_elem.text}")
            cod = cod_elem.text

        # Oxigênio: recalcula hora final
        if cod in CODIGOS_OXIGENIO:
            h_ini = proc.find('ans:horaInicial', NS)
            h_fim = proc.find('ans:horaFinal', NS)
            qtd   = proc.find('ans:quantidadeExecutada', NS)
            if all(x is not None for x in (h_ini, h_fim, qtd)):
                h_fim.text = calcular_hora_fim_oxigenio(h_ini.text, qtd.text, cod)
                log.append(f"[OXIGÊNIO] {cod}: horaFinal → {h_fim.text}")

        # Via de acesso e técnica
        if cod in dicts['procs']:
            regra = dicts['procs'][cod]
            via = limpar_numero(regra.get('Via de Acesso (1, 2 ou EXCLUIR)', ''))
            tec = limpar_numero(regra.get('Técnica (1, 2 ou EXCLUIR)', ''))
            reordenar_proc_executado(proc, via, tec)
            if via:
                log.append(f"[VIA/TÉC] {cod}: via={via}, técnica={tec}")

        # Equipes
        equipes = proc.findall('ans:identEquipe', NS)
        equipes_remover = [eq for eq in equipes if _processar_equipe(eq, cod, is_uberlandia, dicts)]

        if equipes_remover:
            if len(equipes_remover) == len(equipes):
                para_remover.append(proc)
                log.append(f"[REMOÇÃO] Procedimento {cod} removido (todas equipes conveniadas)")
            else:
                for eq in equipes_remover:
                    proc.remove(eq)
                log.append(f"[REMOÇÃO] {len(equipes_remover)} equipe(s) conveniada(s) removida(s) do proc {cod}")

    for p in para_remover:
        procs_container.remove(p)


def _processar_despesas(despesas_container, dicts: dict, log: list):
    """Itera sobre despesas aplicando de-para, oxigênio, unidade e ANVISA."""
    for despesa in despesas_container.findall('ans:despesa', NS):
        servicos = despesa.find('ans:servicosExecutados', NS)
        if servicos is None:
            continue

        cod_elem = servicos.find('ans:codigoProcedimento', NS)
        if cod_elem is None or not cod_elem.text:
            continue

        cod = padronizar_8_digitos(cod_elem.text)

        # De-para
        if cod in dicts['itens']:
            cod_elem.text = dicts['itens'][cod]
            log.append(f"[DE-PARA] Código despesa {cod} → {cod_elem.text}")
            cod = cod_elem.text

        # Oxigênio
        if cod in CODIGOS_OXIGENIO:
            h_ini = servicos.find('ans:horaInicial', NS)
            h_fim = servicos.find('ans:horaFinal', NS)
            qtd   = servicos.find('ans:quantidadeExecutada', NS)
            if all(x is not None for x in (h_ini, h_fim, qtd)):
                h_fim.text = calcular_hora_fim_oxigenio(h_ini.text, qtd.text, cod)
                log.append(f"[OXIGÊNIO] Despesa {cod}: horaFinal → {h_fim.text}")

        # Unidade de medida
        if cod in dicts['unidades']:
            unid_val = dicts['unidades'][cod]
            unid_val = unid_val.zfill(3) if unid_val.isdigit() else unid_val
            unid_elem = servicos.find('ans:unidadeMedida', NS)
            if unid_elem is not None:
                unid_elem.text = unid_val
            else:
                ET.SubElement(servicos, ans_tag('unidadeMedida')).text = unid_val
            log.append(f"[UNIDADE] {cod}: unidade → {unid_val}")

        # ANVISA / Fabricante
        if cod in dicts['anvisa']:
            regra = dicts['anvisa'][cod]
            anvisa_alvo = limpar_numero(regra['Registro ANVISA'])
            ref_alvo    = limpar_numero(regra['Ref. Fabricante'])
            anvisa_el   = servicos.find('ans:registroANVISA', NS)
            ref_el      = servicos.find('ans:codigoRefFabricante', NS)
            vazio       = lambda el: el is None or not (el.text or '').strip()

            if (anvisa_alvo and vazio(anvisa_el)) or (ref_alvo and vazio(ref_el)):
                reordenar_servico_executado(
                    servicos,
                    nova_anvisa=anvisa_alvo if (anvisa_alvo and vazio(anvisa_el)) else None,
                    nova_ref=ref_alvo    if (ref_alvo and vazio(ref_el))    else None,
                )
                log.append(f"[ANVISA] {cod}: ANVISA={anvisa_alvo}, Ref={ref_alvo}")


# MELHORIA 5: Função principal enxuta — só orquestra, não processa
def processar_xml_tiss(arquivo_xml, dfs: dict) -> tuple[bytes, list]:
    """
    Processa o XML TISS e retorna (bytes_corrigidos, log_de_alteracoes).
    """
    # MELHORIA 6: Detecta encoding automaticamente antes de parsear
    raw = arquivo_xml.read()
    arquivo_xml.seek(0)
    encoding = 'ISO-8859-1' if b'ISO-8859-1' in raw[:200] or b'iso-8859-1' in raw[:200] else 'utf-8'

    tree = ET.parse(io.BytesIO(raw))
    root = tree.getroot()
    log  = []

    dicts = construir_dicionarios(dfs)

    for guia in root.findall('.//ans:guiaResumoInternacao', NS):
        carteira = guia.find('.//ans:dadosBeneficiario/ans:numeroCarteira', NS)
        is_ub    = carteira is not None and (carteira.text or '').strip().startswith(PREFIXO_UBERLANDIA)

        procs_cont = guia.find('.//ans:procedimentosExecutados', NS)
        if procs_cont is not None:
            _processar_procedimentos(procs_cont, is_ub, dicts, log)

        desp_cont = guia.find('.//ans:outrasDespesas', NS)
        if desp_cont is not None:
            _processar_despesas(desp_cont, dicts, log)

    # MELHORIA 7: Recálculo do hash MD5 encapsulado em função própria
    xml_bytes = _serializar_e_calcular_hash(tree, root, encoding)
    return xml_bytes, log


def _serializar_e_calcular_hash(tree, root, encoding: str) -> bytes:
    """Serializa a árvore XML, corrige a declaração e recalcula o hash MD5."""
    # Zera o hash antes de serializar
    for elem in root.iter():
        if tag_limpa(elem) == 'hash':
            elem.text = ''
            break

    buf = io.BytesIO()
    tree.write(buf, encoding=encoding, xml_declaration=True, short_empty_elements=False)
    xml_bytes = buf.getvalue()

    # Padroniza aspas na declaração XML
    enc_up = encoding.upper()
    xml_bytes = xml_bytes.replace(
        f"<?xml version='1.0' encoding='{enc_up}'?>".encode(),
        f'<?xml version="1.0" encoding="{enc_up}"?>'.encode(),
    ).replace(
        f"<?xml version='1.0' encoding='{encoding}'?>".encode(),
        f'<?xml version="1.0" encoding="{enc_up}"?>'.encode(),
    )

    # Normaliza quebras de linha para CRLF (padrão ANS)
    xml_bytes = xml_bytes.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')

    md5 = hashlib.md5(xml_bytes).hexdigest()
    xml_bytes = xml_bytes.replace(
        b'<ans:hash></ans:hash>',
        f'<ans:hash>{md5}</ans:hash>'.encode(encoding),
    )
    return xml_bytes


# ==========================================
# INTERFACE GRÁFICA
# ==========================================
st.title("🛠️ Sistema Integrado Cloud TISS - Unimed Uberlândia")
st.caption("Configure as regras operacionais abaixo. Os dados ficam salvos dinamicamente na nuvem.")

# --- SIDEBAR ---
with st.sidebar:
    st.header("☁️ Gerenciamento da Nuvem")

    if st.button("📥 Forçar Sincronização (Nuvem → App)", use_container_width=True):
        carregar_sheets(silencioso=False)
        st.rerun()

    st.divider()
    st.subheader("💾 Salvar Alterações")
    autorizado = st.checkbox("⚠️ Autorizo a gravação e substituição dos dados na nuvem")

    if st.button("☁️ Enviar para o Sheets", type="primary", use_container_width=True, disabled=not autorizado):
        salvar_sheets()

    st.divider()
    st.caption("Backup local de segurança:")
    buf_export = io.BytesIO()
    with pd.ExcelWriter(buf_export, engine='xlsxwriter') as writer:
        for k in ABAS:
            st.session_state[f'tab_{k}'].to_excel(writer, sheet_name=k, index=False)
    st.download_button(
        label="📤 Exportar Regras (Excel)",
        data=buf_export.getvalue(),
        file_name="backup_regras.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )

# --- ABAS PRINCIPAIS ---
(aba_proc, aba_m, aba_p, aba_c,
 aba_b, aba_i, aba_u, aba_a) = st.tabs([
    "🚀 Processar XML", "👥 1. Médicos e CBO",
    "🏥 2. Regras de Procedimento", "🚫 3. Médicos Conveniados",
    "🛡️ 4. Blindagem de Clínicas", "🔄 5. De-Para de Códigos",
    "📦 6. Unidades de Medida", "🩺 7. ANVISA e Fabricante",
])

# ── ABA PROCESSAR ──
with aba_proc:
    st.header("Processamento de Lotes TISS")

    # MELHORIA 8: Upload múltiplo de XMLs em lote
    xmls_up = st.file_uploader(
        "Selecione um ou mais arquivos XML hospitalar",
        type=['xml'],
        accept_multiple_files=True,
    )

    if xmls_up:
        if st.button("▶️ Executar Correções Avançadas", type="primary", use_container_width=True):
            dfs_atuais = {k: st.session_state[f'tab_{k}'] for k in ABAS}
            resultados = []

            barra = st.progress(0, text="Iniciando processamento...")

            for i, xml_up in enumerate(xmls_up):
                barra.progress((i) / len(xmls_up), text=f"Processando {xml_up.name}...")
                try:
                    xml_bytes, log = processar_xml_tiss(xml_up, dfs_atuais)
                    resultados.append({
                        'nome': xml_up.name,
                        'bytes': xml_bytes,
                        'log': log,
                        'ok': True,
                    })
                except Exception as e:
                    resultados.append({'nome': xml_up.name, 'bytes': None, 'log': [], 'ok': False, 'erro': str(e)})

            barra.progress(1.0, text="Concluído!")

            for res in resultados:
                if res['ok']:
                    st.success(f"✅ **{res['nome']}** — {len(res['log'])} alteração(ões) aplicada(s)")

                    # MELHORIA 9: Log detalhado expansível por arquivo
                    if res['log']:
                        with st.expander(f"📋 Ver relatório de alterações — {res['nome']}"):
                            st.code('\n'.join(res['log']), language=None)

                    # MELHORIA 10: Preview do XML corrigido expansível
                    with st.expander(f"🔍 Preview do XML corrigido — {res['nome']}"):
                        st.code(res['bytes'].decode('ISO-8859-1', errors='replace')[:8000], language='xml')

                    st.download_button(
                        label=f"📥 Baixar — {res['nome']}",
                        data=res['bytes'],
                        file_name=f"CORRIGIDO_{res['nome']}",
                        mime="application/octet-stream",
                        key=f"dl_{res['nome']}",
                    )
                else:
                    st.error(f"❌ **{res['nome']}** — Erro: {res.get('erro', 'desconhecido')}")

            # MELHORIA 11: Download de todos os XMLs em lote como ZIP
            if len(resultados) > 1:
                import zipfile
                zip_buf = io.BytesIO()
                with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for res in resultados:
                        if res['ok']:
                            zf.writestr(f"CORRIGIDO_{res['nome']}", res['bytes'])
                st.download_button(
                    label="📦 Baixar TODOS os XMLs corrigidos (.zip)",
                    data=zip_buf.getvalue(),
                    file_name="xmls_corrigidos.zip",
                    mime="application/zip",
                    use_container_width=True,
                )

# ── ABAS DE CONFIGURAÇÃO ──
with aba_m:
    st.subheader("👥 Médicos e CBO")
    st.caption("Cadastre o nome exato do médico conforme consta no XML. O CBO será substituído automaticamente.")
    st.session_state['tab_medicos'] = st.data_editor(
        st.session_state['tab_medicos'], num_rows="dynamic", use_container_width=True
    )

with aba_p:
    st.subheader("🏥 Regras de Procedimento")
    st.caption("Defina grau de participação, via de acesso e técnica por código de procedimento.")
    st.session_state['tab_procedimentos'] = st.data_editor(
        st.session_state['tab_procedimentos'], num_rows="dynamic",
        use_container_width=True, column_config=CONFIG_COLUNAS_TEXTO
    )

with aba_c:
    st.subheader("🚫 Médicos Conveniados")
    st.caption("Médicos conveniados em cartão têm suas equipes removidas das guias de Uberlândia.")
    st.session_state['tab_conveniados'] = st.data_editor(
        st.session_state['tab_conveniados'], num_rows="dynamic", use_container_width=True
    )

with aba_b:
    st.subheader("🛡️ Blindagem de Clínicas")
    st.caption("Código de prestador na operadora que nunca deve ser removido, mesmo sendo conveniado.")
    st.session_state['tab_blindagem'] = st.data_editor(
        st.session_state['tab_blindagem'], num_rows="dynamic",
        use_container_width=True, column_config=CONFIG_COLUNAS_TEXTO
    )

with aba_i:
    st.subheader("🔄 De-Para de Códigos")
    st.caption("Substitui automaticamente códigos incorretos pelo código correto em procedimentos e despesas.")
    st.session_state['tab_itens'] = st.data_editor(
        st.session_state['tab_itens'], num_rows="dynamic",
        use_container_width=True, column_config=CONFIG_COLUNAS_TEXTO
    )

with aba_u:
    st.subheader("📦 Unidades de Medida")
    st.caption("Define a unidade de medida correta para cada código de item de despesa.")
    st.session_state['tab_unidades'] = st.data_editor(
        st.session_state['tab_unidades'], num_rows="dynamic",
        use_container_width=True, column_config=CONFIG_COLUNAS_TEXTO
    )

with aba_a:
    st.subheader("🩺 Registro ANVISA e Fabricante")
    st.caption("Injeta o número de registro ANVISA e referência do fabricante quando ausentes no XML.")
    st.session_state['tab_anvisa'] = st.data_editor(
        st.session_state['tab_anvisa'], num_rows="dynamic",
        use_container_width=True, column_config=CONFIG_COLUNAS_TEXTO
    )
