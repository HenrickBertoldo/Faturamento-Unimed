import hashlib
import streamlit as st
import xml.etree.ElementTree as ET
import pandas as pd
import io
import streamlit.components.v1 as components
from datetime import datetime, timedelta
from streamlit_gsheets import GSheetsConnection

# ==========================================
# CONFIGURAÇÃO DA PÁGINA 
# ==========================================
st.set_page_config(page_title="TISS Cloud", layout="wide", page_icon="☁️")

# CSS apenas para esconder o menu padrão do Streamlit (deixa com cara de software profissional)
st.markdown("""
    <style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}
    </style>
""", unsafe_allow_html=True)

# ==========================================
# CONSTANTES E NAMESPACES TISS
# ==========================================
NS = {'ans': 'http://www.ans.gov.br/padroes/tiss/schemas'}
for k, v in NS.items():
    ET.register_namespace(k, v)
ET.register_namespace('xsi', 'http://www.w3.org/2001/XMLSchema-instance')

def ans_tag(tag_name): return f"{{{NS['ans']}}}{tag_name}"
def tag_limpa(element): return element.tag.split('}')[-1] if '}' in element.tag else element.tag

def limpar_numero(valor):
    v = str(valor).strip()
    if v.lower() in ['nan', 'none', '<na>', '']: return ''
    if v.endswith('.00'): v = v[:-3]
    elif v.endswith('.0'): v = v[:-2]
    return v

# ==========================================
# ESTRUTURA PADRÃO DAS TABELAS
# ==========================================
tabelas_padrao = {
    'medicos': pd.DataFrame(columns=['Nome do Médico', 'CBO Correto', 'Substituir por Cód. Operadora', 'Código na Operadora']),
    'procedimentos': pd.DataFrame(columns=['Código do Procedimento', 'Grau Part Obrigatório', 'Via de Acesso (1, 2 ou EXCLUIR)', 'Técnica (1, 2 ou EXCLUIR)']),
    'conveniados': pd.DataFrame(columns=['Nome do Médico Conveniado']),
    'blindagem': pd.DataFrame(columns=['Código Prestador Protegido']),
    'itens': pd.DataFrame(columns=['Código Incorreto', 'Código Correto']),
    'unidades': pd.DataFrame(columns=['Código do Item', 'Unidade de Medida Correta']),
    'anvisa': pd.DataFrame(columns=['Código do Item', 'Registro ANVISA', 'Ref. Fabricante'])
}

def formatar_tabela_padrao(df):
    for col in df.columns:
        df[col] = df[col].astype(str).str.strip().str.upper()
        df[col] = df[col].replace(['NAN', 'NONE', '<NA>'], '')
    return df

def carregar_do_sheets(silencioso=False):
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        for aba in tabelas_padrao.keys():
            df = conn.read(worksheet=aba, ttl=0, dtype=str)
            if df is not None and not df.empty:
                for col in df.columns: df[col] = df[col].astype(str).apply(limpar_numero)
                st.session_state[f'tab_{aba}'] = formatar_tabela_padrao(df)
            elif f'tab_{aba}' not in st.session_state:
                st.session_state[f'tab_{aba}'] = tabelas_padrao[aba]
        if not silencioso: st.toast("✅ Regras sincronizadas da nuvem!", icon="☁️")
    except Exception as e:
        if not silencioso: st.error(f"Erro na conexão: {e}")
        for aba in tabelas_padrao.keys():
            if f'tab_{aba}' not in st.session_state: st.session_state[f'tab_{aba}'] = tabelas_padrao[aba]

def salvar_no_sheets():
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        for aba in tabelas_padrao.keys():
            df_atual = formatar_tabela_padrao(st.session_state[f'tab_{aba}'].copy())
            if not df_atual.empty:
                for col in df_atual.columns: df_atual[col] = df_atual[col].astype(str).apply(limpar_numero)
                conn.update(worksheet=aba, data=df_atual)
        st.toast("✅ Alterações gravadas na nuvem!", icon="💾")
    except Exception as e:
        st.error(f"Erro ao salvar: {e}")

if "app_inicializado" not in st.session_state:
    with st.spinner("Conectando à base de dados..."): carregar_do_sheets(silencioso=True)
    st.session_state["app_inicializado"] = True

# ==========================================
# MOTOR DE CORREÇÃO DO XML
# ==========================================
def calcular_tempo_oxigenio(hora_ini_str, qtd_executada, tipo_unidade):
    try:
        t_ini = datetime.strptime(hora_ini_str.strip(), "%H:%M:%S")
        qtd = float(qtd_executada.strip())
        if tipo_unidade == '60034335': return (t_ini + timedelta(hours=qtd)).strftime("%H:%M:%S")
        elif tipo_unidade == '60034343': return (t_ini + timedelta(minutes=qtd)).strftime("%H:%M:%S")
        return hora_ini_str
    except: return hora_ini_str

def reordenar_servico_executado(servicos_node, nova_anvisa=None, nova_ref=None):
    valores = {tag_limpa(c): c for c in list(servicos_node)}
    servicos_node.clear()
    ordem_tiss = ['dataExecucao', 'horaInicial', 'horaFinal', 'codigoTabela', 'codigoProcedimento',
                  'quantidadeExecutada', 'unidadeMedida', 'reducaoAcrescimo', 'valorUnitario', 'valorTotal',
                  'descricaoProcedimento', 'registroANVISA', 'codigoRefFabricante']
    for tag in ordem_tiss:
        if tag == 'registroANVISA' and nova_anvisa:
            el = ET.Element(ans_tag('registroANVISA'))
            el.text = nova_anvisa
            servicos_node.append(el)
        elif tag == 'codigoRefFabricante' and nova_ref:
            el = ET.Element(ans_tag('codigoRefFabricante'))
            el.text = nova_ref
            servicos_node.append(el)
        elif tag in valores:
            if tag == 'registroANVISA' and (not valores[tag].text or not valores[tag].text.strip()) and nova_anvisa: valores[tag].text = nova_anvisa
            if tag == 'codigoRefFabricante' and (not valores[tag].text or not valores[tag].text.strip()) and nova_ref: valores[tag].text = nova_ref
            servicos_node.append(valores[tag])

def padronizar_codigo_8_digitos(cod):
    c = limpar_numero(cod)
    return "0" + c if len(c) == 7 and c.isdigit() else c

def processar_xml_tiss(arquivo_xml, dfs):
    auditoria = { 'cbos': 0, 'itens': 0, 'anvisa': 0, 'unidades': 0, 'oxigenio': 0 }
    tree = ET.parse(arquivo_xml)
    root = tree.getroot()
    
    dict_medicos = {str(r['Nome do Médico']).strip().upper(): r for _, r in dfs['medicos'].iterrows()}
    dict_itens = {padronizar_codigo_8_digitos(k): padronizar_codigo_8_digitos(v) for k, v in zip(dfs['itens']['Código Incorreto'], dfs['itens']['Código Correto']) if pd.notna(k)}
    dict_unidades = {padronizar_codigo_8_digitos(r['Código do Item']): limpar_numero(r['Unidade de Medida Correta']) for _, r in dfs['unidades'].iterrows() if pd.notna(r['Código do Item'])}
    dict_anvisa = {padronizar_codigo_8_digitos(r['Código do Item']): r for _, r in dfs['anvisa'].iterrows() if pd.notna(r['Código do Item'])}

    for guia in root.findall('.//ans:guiaResumoInternacao', NS):
        procs_container = guia.find('.//ans:procedimentosExecutados', NS)
        if procs_container is not None:
            for proc_exec in procs_container.findall('ans:procedimentoExecutado', NS):
                equipes = proc_exec.findall('ans:identEquipe', NS)
                for eq in equipes:
                    ident_eq = eq.find('ans:identificacaoEquipe', NS)
                    if ident_eq is None: continue
                    nome_prof_elem = ident_eq.find('ans:nomeProf', NS)
                    nome_prof = nome_prof_elem.text.strip().upper() if nome_prof_elem is not None and nome_prof_elem.text else ""
                    cbo_elem = ident_eq.find('ans:CBOS', NS)

                    if nome_prof in dict_medicos:
                        regra_m = dict_medicos[nome_prof]
                        cbo_novo = limpar_numero(regra_m['CBO Correto'])
                        if cbo_elem is not None and cbo_novo != '':
                            cbo_elem.text = cbo_novo
                            auditoria['cbos'] += 1

        despesas_container = guia.find('.//ans:outrasDespesas', NS)
        if despesas_container is not None:
            for despesa in despesas_container.findall('ans:despesa', NS):
                servicos = despesa.find('ans:servicosExecutados', NS)
                if servicos is not None:
                    cod_item_elem = servicos.find('ans:codigoProcedimento', NS)
                    cod_item = padronizar_codigo_8_digitos(cod_item_elem.text) if cod_item_elem is not None and cod_item_elem.text else ""
                    
                    if cod_item in dict_itens:
                        cod_item_elem.text = dict_itens[cod_item]
                        cod_item = dict_itens[cod_item]
                        auditoria['itens'] += 1

                    if cod_item in ['60034335', '60034343']:
                        h_ini, h_fim, qtd_ex = servicos.find('ans:horaInicial', NS), servicos.find('ans:horaFinal', NS), servicos.find('ans:quantidadeExecutada', NS)
                        if h_ini is not None and h_fim is not None and qtd_ex is not None:
                            h_fim.text = calcular_tempo_oxigenio(h_ini.text, qtd_ex.text, cod_item)
                            auditoria['oxigenio'] += 1

                    if cod_item in dict_unidades:
                        unidade_elem = servicos.find('ans:unidadeMedida', NS)
                        val_unidade = dict_unidades[cod_item].zfill(3) if dict_unidades[cod_item].isdigit() else dict_unidades[cod_item]
                        if unidade_elem is not None: unidade_elem.text = val_unidade
                        else:
                            unidade_elem = ET.Element(ans_tag('unidadeMedida'))
                            unidade_elem.text = val_unidade
                            servicos.append(unidade_elem)
                        auditoria['unidades'] += 1

                    if cod_item in dict_anvisa:
                        regra_a = dict_anvisa[cod_item]
                        anvisa_alvo = limpar_numero(regra_a['Registro ANVISA'])
                        ref_alvo = limpar_numero(regra_a['Ref. Fabricante'])
                        add_anvisa = anvisa_alvo != "" and (servicos.find('ans:registroANVISA', NS) is None or not servicos.find('ans:registroANVISA', NS).text)
                        add_ref = ref_alvo != "" and (servicos.find('ans:codigoRefFabricante', NS) is None or not servicos.find('ans:codigoRefFabricante', NS).text)
                        if add_anvisa or add_ref:
                            reordenar_servico_executado(servicos, anvisa_alvo if add_anvisa else None, ref_alvo if add_ref else None)
                            auditoria['anvisa'] += 1

    hash_node = root.find('.//ans:hash', NS)
    if hash_node is not None: hash_node.text = ""

    temp_buffer = io.BytesIO()
    tree.write(temp_buffer, encoding='ISO-8859-1', xml_declaration=True)
    xml_bytes = temp_buffer.getvalue()
    xml_bytes = xml_bytes.replace(b"<?xml version='1.0' encoding='ISO-8859-1'?>", b'<?xml version="1.0" encoding="ISO-8859-1"?>')
    xml_bytes = xml_bytes.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
    
    md5_hash = hashlib.md5(xml_bytes).hexdigest()
    if hash_node is not None: xml_bytes = xml_bytes.replace(b'<ans:hash></ans:hash>', f'<ans:hash>{md5_hash}</ans:hash>'.encode('ISO-8859-1'))

    return xml_bytes, auditoria

# ==========================================
# INTERFACE GRÁFICA - LAYOUT CORRIGIDO
# ==========================================
st.title("☁️ Sistema Integrado TISS | UNIMED")
st.caption("Automação, correção e validação de faturamento XML em nuvem.")

config_texto_colunas = {
    "Código do Item": st.column_config.TextColumn("Código (Com zeros)"),
    "Código Incorreto": st.column_config.TextColumn("Incorreto"),
    "Código Correto": st.column_config.TextColumn("Correto"),
    "Código Prestador Protegido": st.column_config.TextColumn("Cód. Protegido"),
    "Unidade de Medida Correta": st.column_config.TextColumn("Nova Unidade"),
    "Registro ANVISA": st.column_config.TextColumn("Reg. ANVISA"),
    "Ref. Fabricante": st.column_config.TextColumn("Ref. Fab.")
}

# --- BARRA LATERAL ---
with st.sidebar:
    st.header("Painel de Controle")
    
    with st.container(border=True):
        st.markdown("**🔄 Sincronização**")
        if st.button("📥 Puxar da Nuvem", use_container_width=True):
            carregar_do_sheets()
            st.rerun()

        with st.expander("💾 Gravar Alterações", expanded=False):
            confirmar_salvamento = st.checkbox("Confirmar sobrescrita")
            if st.button("Enviar para Nuvem", type="primary", use_container_width=True, disabled=not confirmar_salvamento):
                salvar_no_sheets()
                st.rerun()
                
    with st.container(border=True):
        st.markdown("**📦 Carga em Massa**")
        st.caption("Suba um arquivo Excel para alimentar as tabelas.")
        planilha_up = st.file_uploader("Upload Excel", type=['xlsx', 'xls'], label_visibility="collapsed")
        if planilha_up:
            if st.button("Importar Planilha", use_container_width=True):
                xls = pd.read_excel(planilha_up, sheet_name=None, dtype=str)
                for aba, df_importado in xls.items():
                    if aba in tabelas_padrao:
                        st.session_state[f'tab_{aba}'] = formatar_tabela_padrao(df_importado)
                st.success("Importado! Clique em 'Gravar Alterações' para salvar.")

# --- ÁREA PRINCIPAL ---
col1, col2 = st.columns([1, 1.2], gap="large")

with col1:
    with st.container(border=True):
        st.markdown("### 1️⃣ Processamento do Lote")
        st.markdown("Arraste o arquivo XML gerado pelo seu sistema aqui.")
        xml_up = st.file_uploader("Arraste o arquivo XML", type=['xml'], label_visibility="collapsed")
        
        if xml_up:
            if st.button("🚀 Iniciar Correção Automática", type="primary", use_container_width=True):
                try:
                    dfs_atuais = {k: st.session_state[f'tab_{k}'] for k in tabelas_padrao.keys()}
                    xml_resultado, auditoria = processar_xml_tiss(xml_up, dfs_atuais)
                    st.session_state['xml_processado'] = xml_resultado
                    st.session_state['auditoria_atual'] = auditoria
                    st.session_state['nome_arquivo_original'] = xml_up.name
                except Exception as e:
                    st.error(f"Falha ao processar: {e}")

with col2:
    if 'xml_processado' in st.session_state:
        with st.container(border=True):
            aud = st.session_state['auditoria_atual']
            st.markdown("### 2️⃣ Resultado da Auditoria")
            
            c1, c2, c3 = st.columns(3)
            c1.metric("👩‍⚕️ CBOs", aud['cbos'])
            c2.metric("🔄 Itens", aud['itens'])
            c3.metric("🩺 ANVISA", aud['anvisa'])
            
            c4, c5, _ = st.columns(3)
            c4.metric("📦 Unidades", aud['unidades'])
            c5.metric("⏱️ Tempos O²", aud['oxigenio'])
            
            st.divider()
            
            st.download_button(
                label="📥 Baixar XML Validado", 
                data=st.session_state['xml_processado'], 
                file_name=f"PRONTO_{st.session_state['nome_arquivo_original']}", 
                mime="application/xml", 
                type="primary",
                use_container_width=True
            )
            
            xml_str = st.session_state['xml_processado'].decode('ISO-8859-1')
            texto_escaped = xml_str.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
            
            # Botão de cópia ajustado para funcionar bem tanto no tema claro quanto escuro (Fundo transparente)
            html_copiar = f"""
            <button id="cpBtn" style="
                width: 100%; background-color: transparent; color: inherit; 
                border: 1px solid rgba(128, 128, 128, 0.5); padding: 10px; border-radius: 6px; 
                cursor: pointer; font-size: 14px; font-weight: 500;
                transition: 0.2s; color: var(--text-color);
            " onmouseover="this.style.backgroundColor='rgba(128, 128, 128, 0.1)'" onmouseout="this.style.backgroundColor='transparent'">
            📋 Copiar Código-Fonte para a Área de Transferência
            </button>
            <script>
            document.getElementById("cpBtn").addEventListener("click", () => {{
                navigator.clipboard.writeText(`{texto_escaped}`).then(() => {{
                    let b = document.getElementById("cpBtn");
                    b.innerText = "✅ Copiado com Sucesso!";
                    b.style.backgroundColor = "rgba(46, 204, 113, 0.2)";
                    b.style.borderColor = "rgba(46, 204, 113, 0.5)";
                    setTimeout(() => {{ 
                        b.innerText = "📋 Copiar Código-Fonte para a Área de Transferência"; 
                        b.style.backgroundColor = "transparent";
                        b.style.borderColor = "rgba(128, 128, 128, 0.5)";
                    }}, 3000);
                }});
            }});
            </script>
            """
            components.html(html_copiar, height=50)
            
            with st.expander("🔍 Inspecionar Código Visualmente"):
                st.code(xml_str, language='xml')
    else:
        with st.container(border=True):
            st.info("Aguardando arquivo XML. Faça o upload na coluna ao lado.")

st.markdown("<br>", unsafe_allow_html=True)

# --- BASE DE DADOS ---
with st.container(border=True):
    st.markdown("### 🛠️ Parametrização e Regras de Negócio")
    abas = st.tabs(["👩‍⚕️ Médicos e CBO", "⚙️ Procedimentos", "🛡️ Blindagem", "💊 Itens e Meds", "📦 Unidades", "🏥 Registro ANVISA"])

    tabelas_nomes = ['medicos', 'procedimentos', 'blindagem', 'itens', 'unidades', 'anvisa']
    for i, aba_nome in enumerate(tabelas_nomes):
        with abas[i]:
            st.session_state[f'tab_{aba_nome}'] = st.data_editor(
                st.session_state[f'tab_{aba_nome}'], 
                num_rows="dynamic", 
                use_container_width=True, 
                column_config=config_texto_colunas,
                height=350
            )
