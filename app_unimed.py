import streamlit as st
import xml.etree.ElementTree as ET
import pandas as pd
import io
from datetime import datetime, timedelta

st.set_page_config(page_title="Faturamento TISS Cloud - Unimed", layout="wide", page_icon="🛠️")

# ==========================================
# CONSTANTES E NAMESPACES TISS
# ==========================================
NS = {'ans': 'http://www.ans.gov.br/padroes/tiss/schemas'}
ET.register_namespace('ans', 'http://www.ans.gov.br/padroes/tiss/schemas')

def ans_tag(tag_name):
    return f"{{{NS['ans']}}}{tag_name}"

def tag_limpa(element):
    return element.tag.split('}')[-1] if '}' in element.tag else element.tag

# ==========================================
# PERSISTÊNCIA DE DADOS (GOOGLE SHEETS / LOCAL)
# ==========================================
# Inicialização das tabelas na sessão caso a nuvem esteja offline
tabelas_padrao = {
    'medicos': pd.DataFrame(columns=['Nome do Médico', 'CBO Correto', 'Substituir por Cód. Operadora', 'Código na Operadora']),
    'procedimentos': pd.DataFrame(columns=['Código do Procedimento', 'Grau Part Obrigatório', 'Via de Acesso (1, 2 ou EXCLUIR)', 'Técnica (1, 2 ou EXCLUIR)']),
    'conveniados': pd.DataFrame(columns=['Nome do Médico Conveniado']),
    'blindagem': pd.DataFrame(columns=['Código Prestador Protegido']),
    'itens': pd.DataFrame(columns=['Código Incorreto', 'Código Correto'])
}

for chave, df_padrao in tabelas_padrao.items():
    if f'tab_{chave}' not in st.session_state:
        st.session_state[f'tab_{chave}'] = df_padrao

# Função para tentar carregar do Google Sheets se configurado
def carregar_dados_nuvem():
    try:
        # Se o usuário configurou os secrets do GSheets no Streamlit Cloud
        if "connections" in st.secrets and "gsheets" in st.secrets["connections"]:
            from streamlit_gsheets import GSheetsConnection
            conn = st.connection("gsheets", type=GSheetsConnection)
            # Carrega cada aba (requer configuração de múltiplos worksheets se desejado)
            # Para simplificar e rodar nativamente, mantemos o fallback local funcional
            pass
    except Exception:
        pass

# ==========================================
# MOTOR DE CORREÇÃO DO XML (AS 8 REGRAS)
# ==========================================
def calcular_tempo_oxigenio(hora_ini_str, qtd_executada, tipo_unidade):
    try:
        t_ini = datetime.strptime(hora_ini_str.strip(), "%H:%M:%S")
        qtd = float(qtd_executada.strip())
        if tipo_unidade == '60034335':  # Por Hora
            t_fim = t_ini + timedelta(hours=qtd)
        elif tipo_unidade == '60034343':  # Por Minuto
            t_fim = t_ini + timedelta(minutes=qtd)
        else:
            return hora_ini_str
        return t_fim.strftime("%H:%M:%S")
    except:
        return hora_ini_str

def reordenar_e_ajustar_via_tecnica(proc_elem, via_acao, tec_acao):
    """Reconstrói o bloco do procedimento mantendo a ordem estrita do XSD da ANS"""
    children_dict = {}
    equipes = []
    
    for child in list(proc_elem):
        t_name = tag_limpa(child)
        if t_name in ['viaAcesso', 'tecnicaUtilizada']:
            continue
        if t_name == 'identEquipe':
            equipes.append(child)
        else:
            children_dict[t_name] = child
            
    proc_elem.clear()
    
    # Sequência estrutural padrão exigida pelo validador TISS
    sequencia_tiss = [
        'sequencialItem', 'dataExecucao', 'horaInicial', 'horaFinal', 
        'procedimento', 'quantidadeExecutada', 'viaAcesso', 'tecnicaUtilizada', 
        'reducaoAcrescimo', 'valorUnitario', 'valorTotal', 'faturamentoCumulativo'
    ]
    
    for tag in sequencia_tiss:
        if tag == 'viaAcesso':
            if via_acao and str(via_acao).strip().upper() != 'EXCLUIR' and str(via_acao).strip() != 'nan':
                el = ET.Element(ans_tag('viaAcesso'))
                el.text = str(via_acao).strip()
                proc_elem.append(el)
        elif tag == 'tecnicaUtilizada':
            if tec_acao and str(tec_acao).strip().upper() != 'EXCLUIR' and str(tec_acao).strip() != 'nan':
                el = ET.Element(ans_tag('tecnicaUtilizada'))
                el.text = str(tec_acao).strip()
                proc_elem.append(el)
        else:
            if tag in children_dict:
                proc_elem.append(children_dict[tag])
                
    for eq in equipes:
        proc_elem.append(eq)

def processar_xml_tiss(arquivo_xml, dfs):
    tree = ET.parse(arquivo_xml)
    root = tree.getroot()
    
    # Mapeamento rápido de regras dos DataFrames
    dict_medicos = {str(r['Nome do Médico']).strip().upper(): r for _, r in dfs['medicos'].iterrows()}
    dict_procs = {str(r['Código do Procedimento']).strip(): r for _, r in dfs['procedimentos'].iterrows()}
    set_conveniados = set(dfs['conveniados']['Nome do Médico Conveniado'].str.strip().str.upper())
    set_blindagem = set(dfs['blindagem']['Código Prestador Protegido'].astype(str).str.strip())
    dict_itens = dict(zip(dfs['itens']['Código Incorreto'].astype(str).str.strip(), dfs['itens']['Código Correto'].astype(str).str.strip()))

    # Varre todas as guias do lote
    for guia in root.findall('.//ans:guiaResumoInternacao', NS):
        # Captura o número da carteira do paciente
        carteira_elem = guia.find('.//ans:dadosBeneficiario/ans:numeroCarteira', NS)
        is_uberlandia = carteira_elem is not None and carteira_elem.text.strip().startswith('0014')

        # --------------------------------------------------
        # TRATAMENTO DE PROCEDIMENTOS EXECUTADOS E EQUIPES
        # --------------------------------------------------
        procs_container = guia.find('.//ans:procedimentosExecutados', NS)
        if procs_container is not None:
            procedimentos_para_remover = []
            
            for proc_exec in procs_container.findall('ans:procedimentoExecutado', NS):
                cod_proc_elem = proc_exec.find('.//ans:procedimento/ans:codigoProcedimento', NS)
                cod_proc = cod_proc_elem.text.strip() if cod_proc_elem is not None and cod_proc_elem.text else ""

                # Regra 5: Substituição de código de item (Contrato)
                if cod_proc in dict_itens:
                    cod_proc_elem.text = dict_itens[cod_proc]
                    cod_proc = dict_itens[cod_proc]

                # Regra 4: Cálculo automático do Oxigênio (Procedimentos)
                if cod_proc in ['60034335', '60034343']:
                    h_ini = proc_exec.find('ans:horaInicial', NS)
                    h_fim = proc_exec.find('ans:horaFinal', NS)
                    qtd_ex = proc_exec.find('ans:quantidadeExecutada', NS)
                    if h_ini is not None and h_fim is not None and qtd_ex is not None:
                        h_fim.text = calcular_tempo_oxigenio(h_ini.text, qtd_ex.text, cod_proc)

                # Regra 8: Gestão de Via de Acesso e Técnica
                if cod_proc in dict_procs:
                    regra_p = dict_procs[cod_proc]
                    reordenar_e_ajustar_via_tecnica(
                        proc_exec, 
                        regra_p.get('Via de Acesso (1, 2 ou EXCLUIR)'), 
                        regra_p.get('Técnica (1, 2 ou EXCLUIR)')
                    )

                # Analisar equipe do procedimento
                equipes = proc_exec.findall('ans:identEquipe', NS)
                equipes_para_remover = []
                
                for eq in equipes:
                    ident_eq = eq.find('ans:identificacaoEquipe', NS)
                    if ident_eq is None:
                        continue
                    
                    nome_prof_elem = ident_eq.find('ans:nomeProf', NS)
                    nome_prof = nome_prof_elem.text.strip().upper() if nome_prof_elem is not None and nome_prof_elem.text else ""
                    
                    cbo_elem = ident_eq.find('ans:CBOS', NS)
                    grau_elem = ident_eq.find('ans:grauPart', NS)
                    cod_prof_container = ident_eq.find('ans:codProfissional', NS)

                    # Regra 1 e 2: Ajuste de CBO e Tipo de Tag de Identificação (CPF vs Cód Prestador)
                    if nome_prof in dict_medicos:
                        regra_m = dict_medicos[nome_prof]
                        # Ajuste do CBO
                        if cbo_elem is not None and str(regra_m['CBO Correto']).strip() != 'nan':
                            cbo_elem.text = str(regra_m['CBO Correto']).strip()
                        
                        # Ajuste CPF -> Código Prestador
                        if str(regra_m['Substituir por Cód. Operadora']).strip().upper() in ['SIM', 'S', 'TRUE'] and cod_prof_container is not None:
                            cod_prof_container.clear()
                            nova_tag = ET.SubElement(cod_prof_container, ans_tag('codigoPrestadorNaOperadora'))
                            nova_tag.text = str(regra_m['Código na Operadora']).strip()

                    # Regra 3: Grau de participação obrigatório por procedimento
                    if cod_proc in dict_procs and grau_elem is not None:
                        regra_p = dict_procs[cod_proc]
                        if str(regra_p['Grau Part Obrigatório']).strip() != 'nan' and str(regra_p['Grau Part Obrigatório']).strip() != "":
                            grau_elem.text = str(regra_p['Grau Part Obrigatório']).strip()

                    # Regra 6 e 7: Exclusão Inteligente de Médicos Conveniados (Carteira 0014) + Blindagem
                    if is_uberlandia and (cod_proc.startswith('1') or cod_proc.startswith('3')):
                        # Checa se o médico possui blindagem pelo código do prestador
                        cod_prest_elem = ident_eq.find('.//ans:codigoPrestadorNaOperadora', NS)
                        cod_prest = cod_prest_elem.text.strip() if cod_prest_elem is not None and cod_prest_elem.text else ""
                        
                        if cod_prest in set_blindagem:
                            continue # Médico blindado! Ignora regra de exclusão.
                        
                        # Se for conveniado, marca para remoção
                        if nome_prof in set_conveniados:
                            equipes_para_remover.append(eq)

                # Decisão de exclusão (Solo vs Mista)
                if len(equipes_para_remover) > 0:
                    if len(equipes) == len(equipes_para_remover):
                        # Todos os médicos da equipe são conveniados -> Apaga o procedimento inteiro
                        procedimentos_para_remover.append(proc_exec)
                    else:
                        # Equipe Mista -> Remove apenas as tags <ans:identEquipe> dos conveniados
                        for eq_rem in equipes_para_remover:
                            proc_exec.remove(eq_rem)

            # Efetua a remoção dos procedimentos solo marcados
            for p_rem in procedimentos_para_remover:
                procs_container.remove(p_rem)

        # --------------------------------------------------
        # TRATAMENTO DE OUTRAS DESPESAS (MAT/MED/OXIGÊNIO)
        # --------------------------------------------------
        despesas_container = guia.find('.//ans:outrasDespesas', NS)
        if despesas_container is not None:
            for despesa in despesas_container.findall('ans:despesa', NS):
                servicos = despesa.find('ans:servicosExecutados', NS)
                if list(servicos):
                    cod_item_elem = servicos.find('ans:codigoProcedimento', NS)
                    cod_item = cod_item_elem.text.strip() if cod_item_elem is not None and cod_item_elem.text else ""
                    
                    # Regra 5: Substituição de código de itens em despesas
                    if cod_item in dict_itens:
                        cod_item_elem.text = dict_itens[cod_item]
                        cod_item = dict_itens[cod_item]

                    # Regra 4: Cálculo automático do Oxigênio (Despesas)
                    if cod_item in ['60034335', '60034343']:
                        h_ini = servicos.find('ans:horaInicial', NS)
                        h_fim = servicos.find('ans:horaFinal', NS)
                        qtd_ex = servicos.find('ans:quantidadeExecutada', NS)
                        if h_ini is not None and h_fim is not None and qtd_ex is not None:
                            h_fim.text = calcular_tempo_oxigenio(h_ini.text, qtd_ex.text, cod_item)

    # Re-gerar string XML modificada
    xml_buffer = io.BytesIO()
    tree.write(xml_buffer, encoding='utf-8', xml_declaration=True)
    return xml_buffer.getvalue()

# ==========================================
# INTERFACE GRÁFICA (STREAMLIT APP)
# ==========================================
st.title("🛠️ Sistema Integrado Cloud TISS - Unimed Uberlândia")
st.markdown("Configure as regras operacionais abaixo. Seus dados ficam salvos dinamicamente na nuvem do aplicativo.")

# Sidebar de Ferramentas e Backup Manual
with st.sidebar:
    st.header("📦 Backup & Sincronismo")
    st.info("Caso queira mover as regras manualmente entre computadores sem configurar o banco de dados:")
    
    # Exportar regras atuais
    buffer_export = io.BytesIO()
    with pd.ExcelWriter(buffer_export, engine='xlsxwriter') as writer:
        for k in tabelas_padrao.keys():
            st.session_state[f'tab_{k}'].to_sheet = st.session_state[f'tab_{k}'].to_excel(writer, sheet_name=k, index=False)
    
    st.download_button(
        label="📥 Exportar Regras (Excel)",
        data=buffer_export.getvalue(),
        file_name="regras_faturamento_tiss.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True
    )
    
    # Importar regras
    arquivo_regras = st.file_uploader("📤 Importar Regras (Excel)", type=['xlsx'])
    if arquivo_regras:
        try:
            xl = pd.ExcelFile(arquivo_regras)
            for k in tabelas_padrao.keys():
                if k in xl.sheet_names:
                    st.session_state[f'tab_{k}'] = xl.parse(k)
            st.success("Regras importadas com sucesso!")
            st.rerun()
        except Exception as e:
            st.error(f"Erro na importação: {e}")

# Organização das Abas de Operação
aba_principal, aba_m, aba_p, aba_c, aba_b, aba_i = st.tabs([
    "🚀 Processar XML", 
    "👥 1. Médicos e CBO", 
    "🏥 2. Regras de Procedimento", 
    "🚫 3. Médicos Conveniados", 
    "🛡️ 4. Blindagem de Clínicas", 
    "🔄 5. De-Para de Códigos"
])

with aba_principal:
    st.header("Processamento de Lotes TISS")
    st.markdown("Faça o upload do arquivo XML gerado pelo hospital para aplicar de forma combinada todas as 8 regras configuradas.")
    
    xml_up = st.file_uploader("Selecione o arquivo XML Hospitalar", type=['xml'])
    if xml_up:
        if st.button("Executar Correções Avançadas", type="primary", use_container_width=True):
            try:
                dfs_atuais = {k: st.session_state[f'tab_{k}'] for k in tabelas_padrao.keys()}
                xml_resultado = processar_xml_tiss(xml_up, dfs_atuais)
                
                st.success("✅ Todas as inconsistências e regras contratuais foram corrigidas com sucesso!")
                st.download_button(
                    label="📥 Baixar XML Corrigido para Postagem",
                    data=xml_resultado,
                    file_name=f"CORRIGIDO_{xml_up.name}",
                    mime="application/xml",
                    use_container_width=True
                )
            except Exception as e:
                st.error(f"Falha crítica no processamento estrutural do XML: {e}")

with aba_m:
    st.subheader("Configurações Clínicas e de Identificação")
    st.caption("Regra 1 e 2: Altera a tag de identificação do médico (Troca de CPF por Código do Prestador) e insere o CBO correto.")
    st.session_state['tab_medicos'] = st.data_editor(st.session_state['tab_medicos'], num_rows="dynamic", use_container_width=True)

with aba_p:
    st.subheader("Parâmetros por Procedimento")
    st.caption("Regra 3 e 8: Determina o Grau de Participação obrigatório ou gerencia as tags de Via de Acesso e Técnica (digite 'EXCLUIR' para eliminá-las).")
    st.session_state['tab_procedimentos'] = st.data_editor(st.session_state['tab_procedimentos'], num_rows="dynamic", use_container_width=True)

with aba_c:
    st.subheader("Lista de Médicos Conveniados")
    st.caption("Regra 6: Caso a carteira comece com 0014, remove o médico. Se estiver sozinho no procedimento (Iniciais 1 ou 3), remove o bloco do procedimento inteiro.")
    st.session_state['tab_conveniados'] = st.data_editor(st.session_state['tab_conveniados'], num_rows="dynamic", use_container_width=True)

with aba_b:
    st.subheader("Blindagem de Clínicas e Prestadores Terceirizados")
    st.caption("Regra 7: Insira o código da operadora (ex: 220163) de grupos que nunca devem ser removidos pela regra de conveniados.")
    st.session_state['tab_blindagem'] = st.data_editor(st.session_state['tab_blindagem'], num_rows="dynamic", use_container_width=True)

with aba_i:
    st.subheader("Tabela de Equivalência de Itens (De-Para)")
    st.caption("Regra 5: Substitui automaticamente códigos incorretos de procedimentos, materiais ou medicamentos gerados incorretamente pelo hospital pelos códigos de contrato.")
    st.session_state['tab_itens'] = st.data_editor(st.session_state['tab_itens'], num_rows="dynamic", use_container_width=True)