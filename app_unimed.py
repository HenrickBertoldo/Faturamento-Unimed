import hashlib
import streamlit as st
import xml.etree.ElementTree as ET
import pandas as pd
import io
from datetime import datetime, timedelta
from streamlit_gsheets import GSheetsConnection

st.set_page_config(page_title="Faturamento TISS Cloud - Unimed", layout="wide", page_icon="🛠️")

# ==========================================
# CONSTANTES E NAMESPACES TISS
# ==========================================
NS = {'ans': 'http://www.ans.gov.br/padroes/tiss/schemas'}
ET.register_namespace('ans', 'http://www.ans.gov.br/padroes/tiss/schemas')
ET.register_namespace('xsi', 'http://www.w3.org/2001/XMLSchema-instance')

def ans_tag(tag_name):
    return f"{{{NS['ans']}}}{tag_name}"

def tag_limpa(element):
    return element.tag.split('}')[-1] if '}' in element.tag else element.tag

def limpar_numero(valor):
    v = str(valor).strip()
    if v.lower() == 'nan' or v.lower() == 'none' or v == '<na>' or v == '':
        return ''
    if v.endswith('.00'):
        return v[:-3]
    if v.endswith('.0'):
        return v[:-2]
    return v

# ==========================================
# PERSISTÊNCIA DE DADOS (GOOGLE SHEETS)
# ==========================================
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

def carregar_do_sheets():
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        for aba in tabelas_padrao.keys():
            df = conn.read(worksheet=aba, ttl=0)
            if not df.empty:
                st.session_state[f'tab_{aba}'] = df
        st.success("✅ Regras carregadas do Google Sheets com sucesso!")
    except Exception as e:
        st.error(f"Erro ao carregar da nuvem. Erro: {e}")

def salvar_no_sheets():
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        for aba in tabelas_padrao.keys():
            df_atual = st.session_state[f'tab_{aba}']
            if not df_atual.empty:
                conn.update(worksheet=aba, data=df_atual)
        st.success("☁️ Todas as regras foram salvas no Google Sheets!")
    except Exception as e:
        st.error(f"Erro ao salvar na nuvem: {e}")

# ==========================================
# MOTOR DE CORREÇÃO DO XML 
# ==========================================
def calcular_tempo_oxigenio(hora_ini_str, qtd_executada, tipo_unidade):
    try:
        t_ini = datetime.strptime(hora_ini_str.strip(), "%H:%M:%S")
        qtd = float(qtd_executada.strip())
        if tipo_unidade == '60034335':
            t_fim = t_ini + timedelta(hours=qtd)
        elif tipo_unidade == '60034343':
            t_fim = t_ini + timedelta(minutes=qtd)
        else:
            return hora_ini_str
        return t_fim.strftime("%H:%M:%S")
    except:
        return hora_ini_str

def reordenar_e_ajustar_via_tecnica(proc_elem, via_acao, tec_acao):
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
    
    sequencia_tiss = [
        'sequencialItem', 'dataExecucao', 'horaInicial', 'horaFinal', 
        'procedimento', 'quantidadeExecutada', 'viaAcesso', 'tecnicaUtilizada', 
        'reducaoAcrescimo', 'valorUnitario', 'valorTotal', 'faturamentoCumulativo'
    ]
    
    for tag in sequencia_tiss:
        if tag == 'viaAcesso':
            if via_acao and via_acao.upper() != 'EXCLUIR' and via_acao != '':
                el = ET.Element(ans_tag('viaAcesso'))
                el.text = via_acao
                proc_elem.append(el)
        elif tag == 'tecnicaUtilizada':
            if tec_acao and tec_acao.upper() != 'EXCLUIR' and tec_acao != '':
                el = ET.Element(ans_tag('tecnicaUtilizada'))
                el.text = tec_acao
                proc_elem.append(el)
        else:
            if tag in children_dict:
                proc_elem.append(children_dict[tag])
                
    for eq in equipes:
        proc_elem.append(eq)

def processar_xml_tiss(arquivo_xml, dfs):
    tree = ET.parse(arquivo_xml)
    root = tree.getroot()
    
    dict_medicos = {str(r['Nome do Médico']).strip().upper(): r for _, r in dfs['medicos'].iterrows()}
    dict_procs = {limpar_numero(r['Código do Procedimento']): r for _, r in dfs['procedimentos'].iterrows()}
    set_conveniados = set(str(x).strip().upper() for x in dfs['conveniados']['Nome do Médico Conveniado'] if pd.notna(x))
    set_blindagem = set(limpar_numero(x) for x in dfs['blindagem']['Código Prestador Protegido'] if pd.notna(x))
    dict_itens = {limpar_numero(k): limpar_numero(v) for k, v in zip(dfs['itens']['Código Incorreto'], dfs['itens']['Código Correto']) if pd.notna(k)}

    for guia in root.findall('.//ans:guiaResumoInternacao', NS):
        carteira_elem = guia.find('.//ans:dadosBeneficiario/ans:numeroCarteira', NS)
        is_uberlandia = carteira_elem is not None and carteira_elem.text.strip().startswith('0014')

        procs_container = guia.find('.//ans:procedimentosExecutados', NS)
        if procs_container is not None:
            procedimentos_para_remover = []
            
            for proc_exec in procs_container.findall('ans:procedimentoExecutado', NS):
                cod_proc_elem = proc_exec.find('.//ans:procedimento/ans:codigoProcedimento', NS)
                cod_proc = cod_proc_elem.text.strip() if cod_proc_elem is not None and cod_proc_elem.text else ""

                if cod_proc in dict_itens:
                    cod_proc_elem.text = dict_itens[cod_proc]
                    cod_proc = dict_itens[cod_proc]

                if cod_proc in ['60034335', '60034343']:
                    h_ini = proc_exec.find('ans:horaInicial', NS)
                    h_fim = proc_exec.find('ans:horaFinal', NS)
                    qtd_ex = proc_exec.find('ans:quantidadeExecutada', NS)
                    if h_ini is not None and h_fim is not None and qtd_ex is not None:
                        h_fim.text = calcular_tempo_oxigenio(h_ini.text, qtd_ex.text, cod_proc)

                if cod_proc in dict_procs:
                    regra_p = dict_procs[cod_proc]
                    reordenar_e_ajustar_via_tecnica(
                        proc_exec, 
                        limpar_numero(regra_p.get('Via de Acesso (1, 2 ou EXCLUIR)')), 
                        limpar_numero(regra_p.get('Técnica (1, 2 ou EXCLUIR)'))
                    )

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

                    if nome_prof in dict_medicos:
                        regra_m = dict_medicos[nome_prof]
                        cbo_novo = limpar_numero(regra_m['CBO Correto'])
                        
                        if cbo_elem is not None and cbo_novo != '':
                            cbo_elem.text = cbo_novo
                        
                        if str(regra_m['Substituir por Cód. Operadora']).strip().upper() in ['SIM', 'S', 'TRUE'] and cod_prof_container is not None:
                            cod_prof_container.clear()
                            nova_tag = ET.SubElement(cod_prof_container, ans_tag('codigoPrestadorNaOperadora'))
                            nova_tag.text = limpar_numero(regra_m['Código na Operadora'])

                    if cod_proc in dict_procs and grau_elem is not None:
                        regra_p = dict_procs[cod_proc]
                        grau_novo = limpar_numero(regra_p['Grau Part Obrigatório'])
                        if grau_novo != "":
                            grau_elem.text = grau_novo.zfill(2)

                    if is_uberlandia and (cod_proc.startswith('1') or cod_proc.startswith('3')):
                        cod_prest_elem = ident_eq.find('.//ans:codigoPrestadorNaOperadora', NS)
                        cod_prest = cod_prest_elem.text.strip() if cod_prest_elem is not None and cod_prest_elem.text else ""
                        
                        if cod_prest in set_blindagem:
                            continue
                        
                        if nome_prof in set_conveniados:
                            equipes_para_remover.append(eq)

                if len(equipes_para_remover) > 0:
                    if len(equipes) == len(equipes_para_remover):
                        procedimentos_para_remover.append(proc_exec)
                    else:
                        for eq_rem in equipes_para_remover:
                            proc_exec.remove(eq_rem)

            for p_rem in procedimentos_para_remover:
                procs_container.remove(p_rem)

        despesas_container = guia.find('.//ans:outrasDespesas', NS)
        if despesas_container is not None:
            for despesa in despesas_container.findall('ans:despesa', NS):
                servicos = despesa.find('ans:servicosExecutados', NS)
                if list(servicos):
                    cod_item_elem = servicos.find('ans:codigoProcedimento', NS)
                    cod_item = cod_item_elem.text.strip() if cod_item_elem is not None and cod_item_elem.text else ""
                    
                    if cod_item in dict_itens:
                        cod_item_elem.text = dict_itens[cod_item]
                        cod_item = dict_itens[cod_item]

                    if cod_item in ['60034335', '60034343']:
                        h_ini = servicos.find('ans:horaInicial', NS)
                        h_fim = servicos.find('ans:horaFinal', NS)
                        qtd_ex = servicos.find('ans:quantidadeExecutada', NS)
                        if h_ini is not None and h_fim is not None and qtd_ex is not None:
                            h_fim.text = calcular_tempo_oxigenio(h_ini.text, qtd_ex.text, cod_item)

    # ==========================================
    # CÁLCULO DO HASH (SIMULANDO O AMBIENTE WINDOWS)
    # ==========================================
    hash_node = None
    for elem in root.iter():
        if tag_limpa(elem) == 'hash':
            hash_node = elem
            hash_node.text = "HASH_PLACEHOLDER_SEGURO"
            break

    temp_buffer = io.BytesIO()
    tree.write(temp_buffer, encoding='iso-8859-1', xml_declaration=True)
    xml_bytes = temp_buffer.getvalue()

    # Ajuste de aspas na declaração XML para agradar ao Validador
    if b"<?xml version='1.0' encoding='iso-8859-1'?>" in xml_bytes:
        xml_bytes = xml_bytes.replace(b"<?xml version='1.0' encoding='iso-8859-1'?>", b'<?xml version="1.0" encoding="ISO-8859-1"?>')

    # O SEGREDO: Força o ficheiro inteiro a usar quebras de linha do Windows (CRLF)
    xml_bytes = xml_bytes.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')

    # Prepara o ficheiro para cálculo deixando a tag hash rigorosamente vazia
    bytes_para_calculo = xml_bytes.replace(b"<ans:hash>HASH_PLACEHOLDER_SEGURO</ans:hash>", b"<ans:hash></ans:hash>")
    
    # Calcula a matemática perfeita
    md5_hash = hashlib.md5(bytes_para_calculo).hexdigest()
    
    # Injeta a Hash final de volta no ficheiro
    final_xml = bytes_para_calculo.replace(b"<ans:hash></ans:hash>", f"<ans:hash>{md5_hash}</ans:hash>".encode('iso-8859-1'))

    return final_xml

# ==========================================
# INTERFACE GRÁFICA
# ==========================================
st.title("🛠️ Sistema Integrado Cloud TISS - Unimed Uberlândia")
st.markdown("Configure as regras operacionais abaixo. Os seus dados ficam salvos dinamicamente na nuvem do aplicativo.")

with st.sidebar:
    st.header("☁️ Conexão em Nuvem")
    
    if st.button("📥 Baixar Regras da Nuvem", type="primary", use_container_width=True):
        carregar_do_sheets()
        st.rerun()

    if st.button("💾 Salvar Alterações na Nuvem", use_container_width=True):
        salvar_no_sheets()
        
    st.divider()
    st.caption("Uso Manual (Plano B):")
    
    buffer_export = io.BytesIO()
    with pd.ExcelWriter(buffer_export, engine='xlsxwriter') as writer:
        for k in tabelas_padrao.keys():
            st.session_state[f'tab_{k}'].to_excel(writer, sheet_name=k, index=False)
    
    st.download_button(
        label="Exportar Regras (Excel)",
        data=buffer_export.getvalue(),
        file_name="backup_emergencia_regras.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True
    )

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
    
    xml_up = st.file_uploader("Selecione o ficheiro XML Hospitalar", type=['xml'])
    if xml_up:
        if st.button("Executar Correções Avançadas", type="primary", use_container_width=True):
            try:
                dfs_atuais = {k: st.session_state[f'tab_{k}'] for k in tabelas_padrao.keys()}
                xml_resultado = processar_xml_tiss(xml_up, dfs_atuais)
                
                st.success("✅ O XML está pronto e o Hash calculado com formato Windows!")
                
                st.download_button(
                    label="📥 Baixar XML Corrigido para Postagem",
                    data=xml_resultado,
                    file_name=f"CORRIGIDO_{xml_up.name}",
                    mime="application/octet-stream", 
                    use_container_width=True
                )
            except Exception as e:
                st.error(f"Falha crítica no processamento: {e}")

with aba_m:
    st.session_state['tab_medicos'] = st.data_editor(st.session_state['tab_medicos'], num_rows="dynamic", use_container_width=True)

with aba_p:
    st.session_state['tab_procedimentos'] = st.data_editor(st.session_state['tab_procedimentos'], num_rows="dynamic", use_container_width=True)

with aba_c:
    st.session_state['tab_conveniados'] = st.data_editor(st.session_state['tab_conveniados'], num_rows="dynamic", use_container_width=True)

with aba_b:
    st.session_state['tab_blindagem'] = st.data_editor(st.session_state['tab_blindagem'], num_rows="dynamic", use_container_width=True)

with aba_i:
    st.session_state['tab_itens'] = st.data_editor(st.session_state['tab_itens'], num_rows="dynamic", use_container_width=True)
