import hashlib
import re
import zipfile
import traceback
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

st.markdown("""
    <style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}
    /* Editor de XML com aparência de editor de código */
    div[data-testid="stTextArea"] textarea {
        font-family: 'Consolas', 'Courier New', monospace !important;
        font-size: 13px !important;
        line-height: 1.4 !important;
        white-space: pre !important;
        overflow-wrap: normal !important;
        overflow-x: scroll !important;
        resize: vertical !important;
    }
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

def indice_apos(parent, elem_ref):
    if elem_ref is None:
        return len(list(parent))
    return list(parent).index(elem_ref) + 1

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
    'troca_equipe_sadt': pd.DataFrame(columns=['Nome Original (Erro)', 'Nome Novo', 'CRM Novo', 'CBO Novo', 'Cód Operadora Novo', 'Grau Part Novo', 'Conselho Novo', 'UF Nova']),
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
        col_upper = col.upper()
        if any(k in col_upper for k in ['CONSELHO', 'UF', 'GRAU PART', 'VIA DE ACESSO', 'TÉCNICA']):
            df[col] = df[col].apply(lambda x: x.zfill(2) if (x.isdigit() and len(x) == 1) else x)
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
        if not silencioso:
            st.error(f"Erro na conexão com o Google Sheets: {e}")
            with st.expander("🔍 Detalhes técnicos do erro"):
                st.code(traceback.format_exc())
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
        st.error(f"Erro ao salvar no Google Sheets: {e}")
        with st.expander("🔍 Detalhes técnicos do erro"):
            st.code(traceback.format_exc())

if "app_inicializado" not in st.session_state:
    with st.spinner("Conectando à base de dados..."): carregar_do_sheets(silencioso=True)
    st.session_state["app_inicializado"] = True

# ==========================================
# MOTOR DE CORREÇÃO DO XML REVISADO 
# ==========================================
def calcular_tempo_oxigenio(hora_ini_str, qtd_executada, tipo_unidade):
    try:
        qtd = float(qtd_executada.strip())
        # Regra especial: quantidade executada = 24 (horas) -> dia inteiro (00:00:00 às 23:59:59)
        if tipo_unidade == '60034335' and qtd == 24:
            return "00:00:00", "23:59:59", True
        t_ini = datetime.strptime(hora_ini_str.strip(), "%H:%M:%S")
        if tipo_unidade == '60034335': return hora_ini_str, (t_ini + timedelta(hours=qtd)).strftime("%H:%M:%S"), True
        elif tipo_unidade == '60034343': return hora_ini_str, (t_ini + timedelta(minutes=qtd)).strftime("%H:%M:%S"), True
        return hora_ini_str, hora_ini_str, True
    except (ValueError, AttributeError, TypeError):
        return hora_ini_str, hora_ini_str, False

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

def corrigir_valores_negativos(root, auditoria):
    logs = []
    for elem in root.iter():
        tag_nome = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
        if tag_nome in ['quantidadeExecutada', 'valorTotal'] and elem.text:
            texto_original = elem.text.strip()
            if texto_original.startswith('-'):
                texto_corrigido = texto_original.lstrip('-')
                elem.text = texto_corrigido
                logs.append(f"Tag <{tag_nome}>: {texto_original} ➔ {texto_corrigido}")
    
    if 'valores_negativos' not in auditoria:
        auditoria['valores_negativos'] = []
    auditoria['valores_negativos'].extend(logs)
    return len(logs)

def corrigir_motivo_encerramento(root, auditoria):
    logs = []
    for elem in root.iter():
        tag_nome = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
        if tag_nome == 'motivoEncerramento' and elem.text:
            if elem.text.strip() == '11':
                elem.text = '12'
                logs.append("Tag <motivoEncerramento>: 11 ➔ 12")
    
    if 'motivo_encerramento' not in auditoria:
        auditoria['motivo_encerramento'] = []
    auditoria['motivo_encerramento'].extend(logs)
    return len(logs)

def _somar_segundos(hora_str, segundos):
    """Soma 'segundos' a um horário HH:MM:SS, com rollover natural de minuto/hora
    (ex: 23:59:59 + 2s = 00:00:01)."""
    t = datetime.strptime(hora_str.strip(), "%H:%M:%S")
    return (t + timedelta(seconds=segundos)).strftime("%H:%M:%S")

def calcular_hash_tiss(root, hash_node):
    """🛠️ ALGORITMO OFICIAL DA ANS para o hash do Padrão TISS (confirmado com a
    Unimed/Validador TISS): NÃO é o MD5 dos bytes do arquivo inteiro — é o MD5
    da CONCATENAÇÃO do conteúdo (sem as tags) de todos os elementos-folha, na
    ordem em que aparecem no documento, usando ISO-8859-1. Tags vazias, ou que
    só contêm espaço/tab/quebra de linha, não entram no cálculo. Como o cálculo
    não depende de nenhum detalhe de formatação/serialização do XML (tags
    autofechadas, indentação, quebra de linha), ele é imune ao tipo de
    divergência de bytes que causava os erros "Hash inválido" na importação."""
    partes = []
    for elem in root.iter():
        if elem is hash_node:
            continue  # o próprio hash é sempre tratado como vazio no cálculo
        if len(list(elem)) > 0:
            continue  # só elementos-folha (sem filhos) contam
        texto = elem.text
        if texto is None or texto.strip() == '':
            continue  # tags vazias ou só com espaço/tab/quebra de linha não contam
        partes.append(texto)
    concatenado = ''.join(partes)
    return hashlib.md5(concatenado.encode('ISO-8859-1')).hexdigest()

def ajustar_horarios_duplicados(procs_container, auditoria):
    """🆕 NOVA REGRA: quando um procedimento é dividido em vários itens de
    quantidadeExecutada=1 (em vez de um único item com quantidade > 1), a
    Unimed rejeita a guia com a crítica "Serviço duplicado" sempre que dois
    ou mais itens têm o mesmo código de procedimento, a mesma data e o mesmo
    horário inicial/final. Esta regra detecta esses grupos e escalona os
    horários em +1s, +2s, +3s... a partir do segundo item do grupo (o
    primeiro item permanece intacto), sem alterar quantidade, valor ou
    qualquer outro dado do procedimento."""
    if 'horarios_duplicados' not in auditoria:
        auditoria['horarios_duplicados'] = []

    grupos = {}
    for proc_exec in procs_container.findall('ans:procedimentoExecutado', NS):
        cod_elem = proc_exec.find('.//ans:codigoProcedimento', NS)
        data_elem = proc_exec.find('ans:dataExecucao', NS)
        h_ini_elem = proc_exec.find('ans:horaInicial', NS)
        h_fim_elem = proc_exec.find('ans:horaFinal', NS)
        if cod_elem is None or data_elem is None or h_ini_elem is None or h_fim_elem is None:
            continue
        if not (cod_elem.text and data_elem.text and h_ini_elem.text and h_fim_elem.text):
            continue
        chave = (padronizar_codigo_8_digitos(cod_elem.text), data_elem.text.strip(), h_ini_elem.text.strip(), h_fim_elem.text.strip())
        grupos.setdefault(chave, []).append((h_ini_elem, h_fim_elem))

    for (cod_p, data_exec, h_ini_orig, h_fim_orig), ocorrencias in grupos.items():
        if len(ocorrencias) < 2:
            continue
        for i, (h_ini_elem, h_fim_elem) in enumerate(ocorrencias[1:], start=1):
            h_ini_elem.text = _somar_segundos(h_ini_orig, i)
            h_fim_elem.text = _somar_segundos(h_fim_orig, i)
        auditoria['horarios_duplicados'].append(
            f"Procedimento {cod_p} em {data_exec}: {len(ocorrencias)} ocorrências no horário {h_ini_orig} "
            f"— {len(ocorrencias) - 1} escalonada(s) em +1s, +2s... para evitar crítica 'Serviço duplicado'."
        )

def recalcular_hash_e_serializar(tree, root):
    """Recebe uma árvore ElementTree já com os dados finais e devolve os bytes
    prontos para gravação: recalcula o hash (algoritmo oficial ANS) e serializa
    em ISO-8859-1 com quebras de linha CRLF. Usada tanto pelo motor automático
    (processar_xml_tiss) quanto pelo salvamento manual no editor de XML —
    garante que os dois caminhos gerem hash de forma idêntica."""
    hash_node = root.find('.//ans:hash', NS)
    if hash_node is not None:
        hash_node.text = ""
        md5_hash = calcular_hash_tiss(root, hash_node)
        hash_node.text = md5_hash

    temp_buffer = io.BytesIO()
    tree.write(temp_buffer, encoding='ISO-8859-1', xml_declaration=True)
    xml_bytes = temp_buffer.getvalue()
    xml_bytes = xml_bytes.replace(b"<?xml version='1.0' encoding='ISO-8859-1'?>", b'<?xml version="1.0" encoding="ISO-8859-1"?>')
    xml_bytes = xml_bytes.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
    return xml_bytes

def validar_e_recalcular_xml_editado(texto_editado):
    """Usada pelo editor manual (Salvar alterações / Localizar e Substituir).
    Tenta interpretar o texto do editor como XML válido e, se conseguir,
    recalcula o hash com a MESMA função usada pelo motor automático.
    Retorna (xml_bytes_ou_None, mensagem_de_erro_ou_None)."""
    try:
        xml_encodado = texto_editado.encode('ISO-8859-1')
    except UnicodeEncodeError as e:
        return None, f"O texto contém um caractere fora do padrão ISO-8859-1 (posição {e.start}: '{texto_editado[e.start:e.start+1]}'). Remova ou substitua esse caractere antes de salvar."

    try:
        root = ET.fromstring(xml_encodado)
    except ET.ParseError as e:
        return None, f"XML inválido — não é possível salvar: {e}"

    tree = ET.ElementTree(root)
    try:
        xml_bytes = recalcular_hash_e_serializar(tree, root)
    except Exception as e:
        return None, f"Falha ao recalcular o hash/serializar o XML: {e}"
    return xml_bytes, None

def _extrair_hash_do_texto(texto):
    """Extrai o valor atualmente escrito dentro de <ans:hash>...</ans:hash> a
    partir do texto bruto (via regex, não exige XML bem formado — útil para
    exibir o hash mesmo enquanto o usuário está editando o XML no meio do
    processo, antes de salvar)."""
    m = re.search(r'<ans:hash>([^<]*)</ans:hash>', texto)
    return m.group(1).strip() if m else None

def processar_xml_tiss(arquivo_xml, dfs):
    auditoria = {
        'cbos': [], 'medicos_trocados': [], 'itens': [], 'anvisa': [], 'unidades': [], 'oxigenio': [],
        'conveniados_excluidos': [], 'procedimentos_ajustados': [], 'guias_blindadas': [], 'erros': [],
        'valores_negativos': [], 'motivo_encerramento': [], 'horarios_duplicados': []
    }
    
    arquivo_xml.seek(0)
    tree = ET.parse(arquivo_xml)
    root = tree.getroot()

    # 1. Regra de Valores Negativos
    corrigir_valores_negativos(root, auditoria)

    # 2. Regra do Motivo de Encerramento 11 ➔ 12
    corrigir_motivo_encerramento(root, auditoria)

    # 3. Carregamento das Tabelas e Dicionários
    df_medicos = dfs.get('medicos', pd.DataFrame()) if isinstance(dfs, dict) else pd.DataFrame()
    dict_medicos = {}
    if df_medicos is not None and not df_medicos.empty:
        for _, r in df_medicos.iterrows():
            nome = str(r.get('Nome do Médico', '')).strip().upper()
            if nome and nome not in ['NAN', 'NONE', '<NA>', '']:
                dict_medicos[nome] = r

    df_equipe_sadt = dfs.get('troca_equipe_sadt', pd.DataFrame()) if isinstance(dfs, dict) else pd.DataFrame()
    dict_equipe_sadt = {}
    if df_equipe_sadt is not None and not df_equipe_sadt.empty:
        for _, r in df_equipe_sadt.iterrows():
            orig = str(r.get('Nome Original (Erro)', '')).strip().upper()
            if orig and orig not in ['NAN', 'NONE', '<NA>', '']:
                dict_equipe_sadt[orig] = {
                    'nome_novo': str(r.get('Nome Novo', '')).strip(),
                    'crm_novo': limpar_numero(r.get('CRM Novo', '')),
                    'cbo_novo': limpar_numero(r.get('CBO Novo', '')),
                    'cod_op_novo': limpar_numero(r.get('Cód Operadora Novo', '')),
                    'grau_novo': limpar_numero(r.get('Grau Part Novo', '')),
                    'conselho_novo': limpar_numero(r.get('Conselho Novo', '')),
                    'uf_nova': limpar_numero(r.get('UF Nova', ''))
                }

    df_conveniados = dfs.get('conveniados', pd.DataFrame()) if isinstance(dfs, dict) else pd.DataFrame()
    set_conveniados = set(df_conveniados['Nome do Médico Conveniado'].dropna().astype(str).str.strip().str.upper()) if not df_conveniados.empty and 'Nome do Médico Conveniado' in df_conveniados.columns else set()

    df_blindagem = dfs.get('blindagem', pd.DataFrame()) if isinstance(dfs, dict) else pd.DataFrame()
    set_blindagem = set(df_blindagem['Código Prestador Protegido'].apply(limpar_numero).dropna()) if not df_blindagem.empty and 'Código Prestador Protegido' in df_blindagem.columns else set()

    df_itens = dfs.get('itens', pd.DataFrame()) if isinstance(dfs, dict) else pd.DataFrame()
    dict_itens = {padronizar_codigo_8_digitos(k): padronizar_codigo_8_digitos(v) for k, v in zip(df_itens['Código Incorreto'], df_itens['Código Correto']) if pd.notna(k)} if not df_itens.empty and 'Código Incorreto' in df_itens.columns else {}

    df_unidades = dfs.get('unidades', pd.DataFrame()) if isinstance(dfs, dict) else pd.DataFrame()
    dict_unidades = {padronizar_codigo_8_digitos(r['Código do Item']): limpar_numero(r['Unidade de Medida Correta']) for _, r in df_unidades.iterrows() if pd.notna(r.get('Código do Item'))} if not df_unidades.empty and 'Código do Item' in df_unidades.columns else {}

    df_anvisa = dfs.get('anvisa', pd.DataFrame()) if isinstance(dfs, dict) else pd.DataFrame()
    dict_anvisa = {padronizar_codigo_8_digitos(r['Código do Item']): r for _, r in df_anvisa.iterrows() if pd.notna(r.get('Código do Item'))} if not df_anvisa.empty and 'Código do Item' in df_anvisa.columns else {}

    df_procedimentos = dfs.get('procedimentos', pd.DataFrame()) if isinstance(dfs, dict) else pd.DataFrame()
    dict_procedimentos = {padronizar_codigo_8_digitos(r['Código do Procedimento']): r for _, r in df_procedimentos.iterrows() if pd.notna(r.get('Código do Procedimento'))} if not df_procedimentos.empty and 'Código do Procedimento' in df_procedimentos.columns else {}

    guias_int = [(g, 'internacao') for g in root.findall('.//ans:guiaResumoInternacao', NS)]
    guias_sadt = [(g, 'sadt') for g in root.findall('.//ans:guiaSP-SADT', NS)]
    todas_guias = guias_int + guias_sadt

    for indice_guia, (guia, tipo_guia) in enumerate(todas_guias, start=1):
        try:
            prestador_elem = guia.find('.//ans:dadosPrestador/ans:codigoPrestadorNaOperadora', NS)
            if prestador_elem is None:
                prestador_elem = guia.find('.//ans:dadosContratado/ans:codigoPrestadorNaOperadora', NS)
            if prestador_elem is not None and limpar_numero(prestador_elem.text) in set_blindagem:
                auditoria['guias_blindadas'].append(f"Guia ignorada (Prestador {limpar_numero(prestador_elem.text)} protegido)")
                continue

            eh_unimed_0014 = False
            if tipo_guia == 'internacao':
                carteira_elem = guia.find('.//ans:dadosBeneficiario/ans:numeroCarteira', NS)
                numero_carteira = limpar_numero(carteira_elem.text) if carteira_elem is not None and carteira_elem.text else ""
                eh_unimed_0014 = numero_carteira.startswith('0014')

            # --- SUBSTITUIÇÃO DE EQUIPE EM GUIAS SADT ---
            if tipo_guia == 'sadt':
                for eq_sadt in guia.findall('.//ans:equipeSadt', NS):
                    nome_prof_elem = eq_sadt.find('ans:nomeProf', NS)
                    if nome_prof_elem is not None and nome_prof_elem.text:
                        nome_orig_xml = nome_prof_elem.text.strip().upper()
                        
                        if nome_orig_xml in dict_equipe_sadt:
                            regra = dict_equipe_sadt[nome_orig_xml]
                            
                            if regra['nome_novo']: nome_prof_elem.text = regra['nome_novo']
                            
                            if regra['crm_novo']:
                                crm_el = eq_sadt.find('ans:numeroConselhoProfissional', NS)
                                if crm_el is not None: crm_el.text = regra['crm_novo']
                                else:
                                    crm_el = ET.Element(ans_tag('numeroConselhoProfissional'))
                                    crm_el.text = regra['crm_novo']
                                    eq_sadt.append(crm_el)
                                    
                            if regra['cbo_novo']:
                                cbos_existentes = [c for c in eq_sadt.iter() if tag_limpa(c) in ['CBOS', 'codigoCBOS', 'codigoCBO']]
                                if cbos_existentes:
                                    cbos_existentes[0].text = regra['cbo_novo']
                                    for c_extra in cbos_existentes[1:]:
                                        for parent in eq_sadt.iter():
                                            if c_extra in list(parent): parent.remove(c_extra)
                                else:
                                    cbo_el = ET.Element(ans_tag('CBOS'))
                                    cbo_el.text = regra['cbo_novo']
                                    eq_sadt.append(cbo_el)
                                    
                            if regra['grau_novo']:
                                grau_el = eq_sadt.find('ans:grauPart', NS)
                                if grau_el is not None: grau_el.text = regra['grau_novo']
                                else:
                                    grau_el = ET.Element(ans_tag('grauPart'))
                                    grau_el.text = regra['grau_novo']
                                    eq_sadt.insert(0, grau_el)
                                    
                            if regra['conselho_novo']:
                                cons_el = eq_sadt.find('ans:conselho', NS)
                                if cons_el is not None: cons_el.text = regra['conselho_novo']
                                else:
                                    cons_el = ET.Element(ans_tag('conselho'))
                                    cons_el.text = regra['conselho_novo']
                                    eq_sadt.append(cons_el)
                                    
                            if regra['uf_nova']:
                                uf_el = eq_sadt.find('ans:UF', NS)
                                if uf_el is not None: uf_el.text = regra['uf_nova']
                                else:
                                    uf_el = ET.Element(ans_tag('UF'))
                                    uf_el.text = regra['uf_nova']
                                    eq_sadt.append(uf_el)
                                    
                            if regra['cod_op_novo']:
                                cod_prof_el = eq_sadt.find('ans:codProfissional', NS)
                                if cod_prof_el is None:
                                    cod_prof_el = ET.Element(ans_tag('codProfissional'))
                                    eq_sadt.append(cod_prof_el)
                                
                                op_el = cod_prof_el.find('ans:codigoPrestadorNaOperadora', NS)
                                if op_el is not None: op_el.text = regra['cod_op_novo']
                                else:
                                    op_el = ET.Element(ans_tag('codigoPrestadorNaOperadora'))
                                    op_el.text = regra['cod_op_novo']
                                    cod_prof_el.append(op_el)
                                    
                            auditoria['medicos_trocados'].append(f"Guia SADT (Equipe Completa): Mapeamento de '{nome_orig_xml}' substituído com sucesso.")

            # --- PROCEDIMENTOS E CBOS DE MÉDICOS ---
            procs_container = guia.find('.//ans:procedimentosExecutados', NS)
            if procs_container is not None:
                procs_para_remover = []
                
                for proc_exec in procs_container.findall('ans:procedimentoExecutado', NS):
                    cod_proc_elem = proc_exec.find('.//ans:codigoProcedimento', NS)
                    cod_p = padronizar_codigo_8_digitos(cod_proc_elem.text) if cod_proc_elem is not None and cod_proc_elem.text else ""
                    
                    is_protected = cod_p.startswith(('4', '2', '04', '02'))
                    equipes_iniciais = proc_exec.findall('ans:identEquipe', NS) + proc_exec.findall('ans:equipeSadt', NS)
                    equipes_remover = []
                    
                    for eq in equipes_iniciais:
                        nome_prof_elem = eq.find('.//ans:nomeProf', NS)
                        nome_prof = nome_prof_elem.text.strip().upper() if nome_prof_elem is not None and nome_prof_elem.text else ""
                        
                        if tipo_guia == 'internacao' and eh_unimed_0014 and nome_prof in set_conveniados:
                            if not is_protected:
                                equipes_remover.append(eq)
                                auditoria['conveniados_excluidos'].append(f"Removido médico(a) '{nome_prof}' do procedimento {cod_p} (Carteira: {numero_carteira})")
                    
                    for eq in equipes_remover:
                        proc_exec.remove(eq)
                    
                    equipes_restantes = proc_exec.findall('ans:identEquipe', NS) + proc_exec.findall('ans:equipeSadt', NS)
                    if len(equipes_iniciais) > 0 and len(equipes_restantes) == 0:
                        procs_para_remover.append(proc_exec)
                        continue 
                    
                    if cod_p in dict_procedimentos:
                        regra_p = dict_procedimentos[cod_p]
                        detalhes_proc = []
                        
                        grau_val = limpar_numero(regra_p.get('Grau Part Obrigatório', ''))
                 # ==========================================
# EDITOR DE XML — INTERFACE DESKTOP-LIKE
# ==========================================
if 'resultados_lote' in st.session_state and st.session_state['resultados_lote'] and not resultado.get('falha_total'):
    st.divider()
    
    nome_arquivo = resultado['nome']
    lote_id = st.session_state.get('lote_id', 0)
    editor_key = f"editor_texto_{lote_id}_{nome_arquivo}"
    baseline_key = f"editor_baseline_{lote_id}_{nome_arquivo}"
    hash_original_key = f"hash_original_{lote_id}_{nome_arquivo}"
    hash_atual_key = f"hash_atual_{lote_id}_{nome_arquivo}"
    erro_validacao_key = f"erro_validacao_{lote_id}_{nome_arquivo}"

    # 1. Inicialização de Estado
    if editor_key not in st.session_state:
        st.session_state[editor_key] = xml_texto
        st.session_state[baseline_key] = xml_texto
        st.session_state[hash_original_key] = _extrair_hash_do_texto(xml_texto)
        st.session_state[hash_atual_key] = st.session_state[hash_original_key]
        st.session_state[erro_validacao_key] = None

    texto_atual = st.session_state[editor_key]
    alterado = texto_atual != st.session_state[baseline_key]
    
    # 2. Cabeçalho (Estilo Janela de Software)
    marcador_alt = "*" if alterado else ""
    st.markdown(f"### 💻 Validador TISS &nbsp; | &nbsp; `{nome_arquivo} {marcador_alt}`")
    
    with st.container(border=True):
        # 3. Barra de Ferramentas Superior
        tb1, tb2, tb3, tb4, tb5, tb6, tb7 = st.columns([1, 1, 1, 1, 2, 2, 1.5], vertical_alignment="bottom")
        
        with tb1:
            if st.button("💾 Salvar", use_container_width=True, disabled=not alterado, help="Salvar alterações"):
                novos_bytes, erro = validar_e_recalcular_xml_editado(texto_atual)
                if erro:
                    st.session_state[erro_validacao_key] = erro
                    st.rerun()
                else:
                    st.session_state[erro_validacao_key] = None
                    novo_texto_final = novos_bytes.decode('ISO-8859-1')
                    st.session_state[editor_key] = novo_texto_final
                    st.session_state[baseline_key] = novo_texto_final
                    st.session_state[hash_atual_key] = _extrair_hash_do_texto(novo_texto_final)
                    resultado['xml_bytes'] = novos_bytes
                    st.rerun()
        with tb2:
            st.download_button("📥 Baixar", data=resultado['xml_bytes'], file_name=f"FINAL_{nome_arquivo}", mime="application/xml", use_container_width=True)
        with tb3:
            if st.button("↶ Reset", use_container_width=True, help="Desfazer alterações não salvas"):
                st.session_state[editor_key] = st.session_state[baseline_key]
                st.session_state[erro_validacao_key] = None
                st.rerun()
        with tb4:
            st.button("✓ Validar", use_container_width=True, disabled=True, help="O XML é validado automaticamente ao salvar")
            
        with tb5:
            loc_text = st.text_input("Localizar", label_visibility="collapsed", placeholder="🔍 Localizar termo...", key=f"loc_{lote_id}")
        with tb6:
            sub_text = st.text_input("Substituir", label_visibility="collapsed", placeholder="⇄ Substituir por...", key=f"sub_{lote_id}")
        with tb7:
            if st.button("🔁 Substituir", use_container_width=True):
                if loc_text:
                    st.session_state[editor_key] = st.session_state[editor_key].replace(loc_text, sub_text)
                    st.rerun()

        # 4. Layout Principal Lado a Lado (Editor 75% | Log 25%)
        st.markdown("<hr style='margin: 0.5rem 0'>", unsafe_allow_html=True)
        ed_col, log_col = st.columns([3, 1], gap="small")
        
        with ed_col:
            st.text_area(
                "Editor XML",
                value=st.session_state[editor_key],
                key=editor_key,
                height=650,
                label_visibility="collapsed"
            )
            
        with log_col:
            st.markdown("##### 📝 Alterações")
            if alterado:
                # Motor de Comparação (Diff) em Python Puro
                linhas_orig = st.session_state[baseline_key].splitlines()
                linhas_edit = st.session_state[editor_key].splitlines()
                mudancas = []
                max_linhas = max(len(linhas_orig), len(linhas_edit))
                
                for i in range(max_linhas):
                    o_lin = linhas_orig[i].strip() if i < len(linhas_orig) else ""
                    e_lin = linhas_edit[i].strip() if i < len(linhas_edit) else ""
                    if o_lin != e_lin:
                        mudancas.append((i+1, o_lin, e_lin))
                
                st.caption(f"🔴 {len(mudancas)} linha(s) modificada(s)")
                
                with st.container(height=580): # Painel com rolagem independente
                    for num_linha, antes, depois in mudancas:
                        st.markdown(f"**Linha {num_linha}**")
                        if antes: st.markdown(f"<span style='color: #dc3545; text-decoration: line-through; font-size: 13px;'>{antes[:40]}{'...' if len(antes)>40 else ''}</span>", unsafe_allow_html=True)
                        if depois: st.markdown(f"<span style='color: #198754; font-size: 13px;'>{depois[:40]}{'...' if len(depois)>40 else ''}</span>", unsafe_allow_html=True)
                        st.markdown("<hr style='margin: 0.5rem 0'>", unsafe_allow_html=True)
            else:
                st.success("✓ Nenhuma alteração.")
                st.caption("O arquivo no editor está idêntico à versão original salva.")

        # 5. Barra de Status Inferior (Status Bar)
        st.markdown("<hr style='margin: 0.5rem 0'>", unsafe_allow_html=True)
        linhas_totais = len(st.session_state[editor_key].splitlines())
        status_msg = f"❌ Erro: {st.session_state[erro_validacao_key]}" if st.session_state[erro_validacao_key] else ("⚠ Alterações não salvas" if alterado else "✓ XML Válido")
        
        st.markdown(f"""
        <div style='font-size: 13px; color: #495057; display: flex; justify-content: space-between; padding: 4px 8px; background-color: #e9ecef; border-radius: 4px; font-family: monospace;'>
            <span>{status_msg} &nbsp;|&nbsp; {linhas_totais} linhas &nbsp;|&nbsp; Codificação: ISO-8859-1</span>
            <span>Hash: <strong>{st.session_state[hash_atual_key]}</strong></span>
        </div>
        """, unsafe_allow_html=True)
                        cod_item_elem = servicos.find('.//ans:codigoProcedimento', NS)
                        cod_item = padronizar_codigo_8_digitos(cod_item_elem.text) if cod_item_elem is not None and cod_item_elem.text else ""
                        cod_original_log = cod_item
                        
                        if cod_item in dict_itens:
                            cod_novo = dict_itens[cod_item]
                            cod_item_elem.text = cod_novo
                            cod_item = cod_novo
                            auditoria['itens'].append(f"Item alterado de {cod_original_log} para {cod_novo}")

                        if cod_item in ['60034335', '60034343']:
                            h_ini, h_fim, qtd_ex = servicos.find('ans:horaInicial', NS), servicos.find('ans:horaFinal', NS), servicos.find('ans:quantidadeExecutada', NS)
                            if h_ini is not None and h_fim is not None and qtd_ex is not None:
                                h_ini_novo, h_fim_novo, ok = calcular_tempo_oxigenio(h_ini.text, qtd_ex.text, cod_item)
                                if ok:
                                    if h_ini.text != h_ini_novo or h_fim.text != h_fim_novo:
                                        auditoria['oxigenio'].append(f"Oxigênio {cod_item}: Hora Inicial/Final ajustadas para {h_ini_novo} / {h_fim_novo}")
                                    h_ini.text = h_ini_novo
                                    h_fim.text = h_fim_novo
                                else:
                                    auditoria['erros'].append(f"Item {cod_item}: não foi possível recalcular hora de O² (horaInicial='{h_ini.text}', qtd='{qtd_ex.text}') — mantido valor original")

                        if cod_item in dict_unidades:
                            unidade_elem = servicos.find('ans:unidadeMedida', NS)
                            val_unidade = dict_unidades[cod_item].zfill(3) if dict_unidades[cod_item].isdigit() else dict_unidades[cod_item]
                            if unidade_elem is not None: unidade_elem.text = val_unidade
                            else:
                                unidade_elem = ET.Element(ans_tag('unidadeMedida'))
                                unidade_elem.text = val_unidade
                                servicos.append(unidade_elem)
                            auditoria['unidades'].append(f"Item {cod_item}: Unidade ajustada para {val_unidade}")

                        if cod_item in dict_anvisa:
                            regra_a = dict_anvisa[cod_item]
                            anvisa_alvo = limpar_numero(regra_a['Registro ANVISA'])
                            ref_alvo = limpar_numero(regra_a['Ref. Fabricante'])
                            add_anvisa = anvisa_alvo != "" and (servicos.find('ans:registroANVISA', NS) is None or not servicos.find('ans:registroANVISA', NS).text)
                            add_ref = ref_alvo != "" and (servicos.find('ans:codigoRefFabricante', NS) is None or not servicos.find('ans:codigoRefFabricante', NS).text)
                            if add_anvisa or add_ref:
                                reordenar_servico_executado(servicos, anvisa_alvo if add_anvisa else None, ref_alvo if add_ref else None)
                                detalhes_anv = []
                                if add_anvisa: detalhes_anv.append(f"ANVISA {anvisa_alvo}")
                                if add_ref: detalhes_anv.append(f"Ref {ref_alvo}")
                                auditoria['anvisa'].append(f"Item {cod_item}: Inserido " + " e ".join(detalhes_anv))

        except Exception as e:
            auditoria['erros'].append(f"Guia #{indice_guia} ({tipo_guia}): erro ao processar — {e}")

    # --- RECALCULO DE HASH (algoritmo OFICIAL da ANS: MD5 da concatenação do
    # conteúdo dos elementos-folha, na ordem do documento — não depende de
    # nenhum detalhe de formatação/serialização do XML) ---
    xml_bytes = recalcular_hash_e_serializar(tree, root)

    return xml_bytes, auditoria

# ==========================================
# INTERFACE GRÁFICA
# ==========================================
st.title("☁️ Sistema Integrado TISS | UNIMED")
st.caption("Automação, correção e validação de faturamento XML em nuvem.")

config_texto_colunas = {
    "Nome Original (Erro)": st.column_config.TextColumn("Nome Sem Cadastro"),
    "Nome Novo": st.column_config.TextColumn("Nome Substituto"),
    "CRM Novo": st.column_config.TextColumn("CRM Substituto"),
    "CBO Novo": st.column_config.TextColumn("CBO Novo"),
    "Cód Operadora Novo": st.column_config.TextColumn("Cód. Operadora"),
    "Grau Part Novo": st.column_config.TextColumn("Grau Part (Ex: 12)"),
    "Conselho Novo": st.column_config.TextColumn("Conselho (Ex: 06)"),
    "UF Nova": st.column_config.TextColumn("UF (Ex: 31)"),
    "Código do Item": st.column_config.TextColumn("Código (Com zeros)"),
    "Código Incorreto": st.column_config.TextColumn("Incorreto"),
    "Código Correto": st.column_config.TextColumn("Correto"),
    "Código Prestador Protegido": st.column_config.TextColumn("Cód. Protegido"),
    "Unidade de Medida Correta": st.column_config.TextColumn("Nova Unidade"),
    "Registro ANVISA": st.column_config.TextColumn("Reg. ANVISA"),
    "Ref. Fabricante": st.column_config.TextColumn("Ref. Fab.")
}

with st.container(border=True):
    st.markdown("### 🔄 Central de Sincronização e Controle de Dados")
    c_sync1, c_sync2, c_sync3 = st.columns([1, 1.2, 1.3], gap="medium")
    
    with c_sync1:
        st.markdown("**1️⃣ Puxar Configurações**")
        if st.button("📥 Puxar Regras da Nuvem", use_container_width=True):
            carregar_do_sheets()
            st.rerun()
            
    with c_sync2:
        st.markdown("**2️⃣ Salvar Novas Configurações**")
        confirmar_salvamento = st.checkbox("Confirmar atualização no Google Sheets")
        if st.button("💾 Gravar Alterações na Nuvem", type="primary", use_container_width=True, disabled=not confirmar_salvamento):
            salvar_no_sheets()
            st.rerun()
            
    with c_sync3:
        st.markdown("**3️⃣ Carga em Massa (Opcional)**")
        planilha_up = st.file_uploader("Upload Excel (.xlsx)", type=['xlsx', 'xls'], label_visibility="collapsed")
        if planilha_up:
            if st.button("Importar Planilha Completa", use_container_width=True):
                xls = pd.read_excel(planilha_up, sheet_name=None, dtype=str)
                for aba, df_importado in xls.items():
                    if aba in tabelas_padrao: st.session_state[f'tab_{aba}'] = formatar_tabela_padrao(df_importado)
                st.success("Tabelas alimentadas! Marque a confirmação e clique em 'Gravar Alterações na Nuvem'.")

st.divider()

TITULOS_AMIGAVEIS_AUDITORIA = {
    'medicos_trocados': '🔀 Médicos e CRMs Substituídos',
    'cbos': '👩‍⚕️ Médicos e CBOs Alterados',
    'itens': '🔄 Itens e Medicamentos Traduzidos',
    'anvisa': '🩺 Registros ANVISA Inseridos',
    'unidades': '📦 Unidades de Medida Ajustadas',
    'oxigenio': '⏱️ Tempos de Oxigênio Recalculados',
    'conveniados_excluidos': '🤝 Médicos Conveniados Removidos',
    'procedimentos_ajustados': '⚙️ Procedimentos Ajustados (Grau/Via/Técnica)',
    'guias_blindadas': '🛡️ Guia(s) Blindada(s)',
    'erros': '⚠️ Avisos e Erros Durante o Processamento',
    'valores_negativos': '➖ Valores Negativos Corrigidos',
    'motivo_encerramento': '🚪 Motivo de Encerramento (11 ➔ 12)',
    'horarios_duplicados': '⏰ Horários Escalonados (Anti-Duplicidade)'
}

def botao_copiar_codigo(xml_str, key_sufixo):
    texto_escaped = xml_str.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
    html_copiar = f"""
    <button id="cpBtn_{key_sufixo}" style="
        width: 100%; background-color: #FFFFFF; color: #1E1E1E; 
        border: 1px solid #CCCCCC; padding: 10px; border-radius: 6px; 
        cursor: pointer; font-size: 14px; font-weight: 600;
        transition: 0.2s; box-shadow: 0px 2px 4px rgba(0,0,0,0.1);
    " onmouseover="this.style.backgroundColor='#F5F5F5'" onmouseout="this.style.backgroundColor='#FFFFFF'">
    📋 Copiar XML
    </button>
    <script>
    document.getElementById("cpBtn_{key_sufixo}").addEventListener("click", () => {{
        navigator.clipboard.writeText(`{texto_escaped}`).then(() => {{
            let b = document.getElementById("cpBtn_{key_sufixo}");
            b.innerText = "✅ Código-Fonte Copiado!";
            b.style.backgroundColor = "#D4EDDA";
            b.style.color = "#155724";
            b.style.borderColor = "#C3E6CB";
            setTimeout(() => {{ 
                b.innerText = "📋 Copiar XML"; 
                b.style.backgroundColor = "#FFFFFF";
                b.style.color = "#1E1E1E";
                b.style.borderColor = "#CCCCCC";
            }}, 3000);
        }});
    }});
    </script>
    """
    components.html(html_copiar, height=50)

# --- ESTRUTURA DAS COLUNAS DA INTERFACE ---
col1, col2 = st.columns([1, 1])

with col1:
    tem_resultados = 'resultados_lote' in st.session_state and bool(st.session_state.get('resultados_lote'))
    titulo_upload = "📁 Trocar arquivos / Novo lote" if tem_resultados else "📜 Processamento de XMLs em Lote"

    # 📦 Recolhido por padrão assim que já existem resultados na tela — libera
    # espaço vertical na coluna, já que o upload deixa de ser a ação principal
    # depois que o lote já foi processado.
    with st.expander(titulo_upload, expanded=not tem_resultados):
        st.markdown("Arraste um ou vários arquivos XML gerados pelo seu sistema aqui.")
        xml_up = st.file_uploader(
            "Arraste os arquivos XML", type=['xml'], label_visibility="collapsed",
            accept_multiple_files=True
        )

        if xml_up:
            st.caption(f"{len(xml_up)} arquivo(s) selecionado(s).")
            if st.button("🚀 Iniciar Correção Automática", type="primary", use_container_width=True):
                dfs_atuais = {k: st.session_state[f'tab_{k}'] for k in tabelas_padrao.keys()}
                resultados_lote = []
                barra = st.progress(0.0, text="Processando arquivos...")
                for i, arquivo in enumerate(xml_up):
                    resultado = {'nome': arquivo.name, 'xml_bytes': None, 'auditoria': None, 'falha_total': None}
                    try:
                        xml_resultado, auditoria = processar_xml_tiss(arquivo, dfs_atuais)
                        resultado['xml_bytes'] = xml_resultado
                        resultado['auditoria'] = auditoria
                    except Exception as e:
                        resultado['falha_total'] = str(e)
                    resultados_lote.append(resultado)
                    barra.progress((i + 1) / len(xml_up), text=f"Processando arquivos... ({i+1}/{len(xml_up)})")
                barra.empty()
                st.session_state['resultados_lote'] = resultados_lote
                st.session_state['lote_id'] = st.session_state.get('lote_id', 0) + 1
                st.rerun()

with col2:
    if 'resultados_lote' in st.session_state and st.session_state['resultados_lote']:
        resultados = st.session_state['resultados_lote']
        
        with st.container(border=True):
            st.markdown("### 📊 Resultado da Auditoria")

            if len(resultados) > 1:
                nomes_arquivos = [r['nome'] for r in resultados]
                
                zip_buffer = io.BytesIO()
                with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
                    for res in resultados:
                        if res['xml_bytes']:
                            zip_file.writestr(f"PRONTO_{res['nome']}", res['xml_bytes'])
                zip_buffer.seek(0)

                st.download_button(
                    label="📦 Baixar Todos os XMLs Corrigidos (.ZIP)",
                    data=zip_buffer,
                    file_name="XMLS_CORRIGIDOS.zip",
                    mime="application/zip",
                    type="primary",
                    use_container_width=True
                )
                
                st.divider()

                nome_selecionado = st.selectbox(
                    "🔍 Selecione o arquivo para visualizar os detalhes:",
                    options=nomes_arquivos
                )
                resultado = next(r for r in resultados if r['nome'] == nome_selecionado)
            else:
                resultado = resultados[0]

            if resultado.get('falha_total'):
                st.error(f"❌ **{resultado['nome']}**: {resultado['falha_total']}")
            else:
                st.success(f"✅ Arquivo **{resultado['nome']}** processado com sucesso!")

                xml_bytes = resultado['xml_bytes']
                try:
                    xml_texto = xml_bytes.decode('ISO-8859-1')
                except (UnicodeDecodeError, AttributeError):
                    xml_texto = xml_bytes.decode('utf-8', errors='replace') if isinstance(xml_bytes, bytes) else xml_bytes

                aud = resultado.get('auditoria', {})

                st.divider()
                st.markdown("#### 📈 Resumo das Alterações")
                
                c1_m, c2_m, c3_m = st.columns(3)
                c1_m.metric("🔀 Médicos Trocados", len(aud.get('medicos_trocados', [])))
                c2_m.metric("👩‍⚕️ CBOs / Códs", len(aud.get('cbos', [])))
                c3_m.metric("➖ Valores Negativos", len(aud.get('valores_negativos', [])))

                c4_m, c5_m, c6_m = st.columns(3)
                c4_m.metric("🚪 Motivo Enc. (11➔12)", len(aud.get('motivo_encerramento', [])))
                c5_m.metric("🔄 Itens Traduzidos", len(aud.get('itens', [])))
                c6_m.metric("📦 Unid. Medida", len(aud.get('unidades', [])))

                c7_m, c8_m, c9_m = st.columns(3)
                c7_m.metric("⏱️ Tempos O²", len(aud.get('oxigenio', [])))
                c8_m.metric("⚙️ Procs. Ajustados", len(aud.get('procedimentos_ajustados', [])))
                c9_m.metric("🛡️ Guia(s) Blindada(s)", len(aud.get('guias_blindadas', [])))

                if aud.get('erros'):
                    st.warning(f"⚠️ {len(aud['erros'])} aviso(s)/erro(s) pontual(is) durante o processamento.")
    else:
        with st.container(border=True):
            st.info("Aguardando arquivo(s) XML. Faça o upload na coluna ao lado.")

# ==========================================
# EDITOR DE XML — seção em LARGURA TOTAL (fora das duas colunas), para dar
# bem mais espaço de trabalho ao editor e ao Localizar/Substituir do que a
# metade da tela que a coluna oferecia.
# ==========================================
if 'resultados_lote' in st.session_state and st.session_state['resultados_lote'] and not resultado.get('falha_total'):
    st.divider()
    with st.container(border=True):
        # ==========================================
        # EDITOR DE XML — edição direta dentro do Streamlit
        # ==========================================
        nome_arquivo = resultado['nome']
        lote_id = st.session_state.get('lote_id', 0)
        editor_key = f"editor_texto_{lote_id}_{nome_arquivo}"
        baseline_key = f"editor_baseline_{lote_id}_{nome_arquivo}"
        hash_original_key = f"hash_original_{lote_id}_{nome_arquivo}"
        hash_atual_key = f"hash_atual_{lote_id}_{nome_arquivo}"
        ja_salvou_key = f"ja_salvou_{lote_id}_{nome_arquivo}"
        proximo_idx_key = f"proximo_idx_{lote_id}_{nome_arquivo}"
        erro_validacao_key = f"erro_validacao_{lote_id}_{nome_arquivo}"

        # Inicialização (uma única vez, na primeira vez que este arquivo é exibido)
        if editor_key not in st.session_state:
            st.session_state[editor_key] = xml_texto
            st.session_state[baseline_key] = xml_texto
            st.session_state[hash_original_key] = _extrair_hash_do_texto(xml_texto)
            st.session_state[hash_atual_key] = st.session_state[hash_original_key]
            st.session_state[ja_salvou_key] = False
            st.session_state[proximo_idx_key] = 0
            st.session_state[erro_validacao_key] = None

        st.markdown("#### ✏️ Editor de XML")
        st.caption("Edite o conteúdo diretamente aqui. As alterações só valem para o arquivo final depois de clicar em **Salvar alterações**. Arraste o cantinho inferior direito da caixa para redimensionar.")

        # Reserva o espaço visual do editor no topo. Ele só é efetivamente
        # desenhado mais abaixo (depois da lógica de Localizar/Substituir/Salvar),
        # para que os botões possam atualizar seu conteúdo dentro do mesmo clique.
        editor_placeholder = st.empty()

        st.markdown("#### 🔎 Localizar e Substituir")
        col_loc1, col_loc2 = st.columns(2)
        with col_loc1:
            termo_localizar = st.text_input("Localizar", key=f"loc_localizar_{lote_id}_{nome_arquivo}")
        with col_loc2:
            termo_substituir = st.text_input("Substituir por", key=f"loc_substituir_{lote_id}_{nome_arquivo}")

        col_b1, col_b2, col_b3 = st.columns(3)
        with col_b1:
            clicou_localizar = st.button("🔍 Localizar", key=f"btn_localizar_{lote_id}_{nome_arquivo}", use_container_width=True)
        with col_b2:
            clicou_proximo = st.button("⏭️ Substituir próximo", key=f"btn_proximo_{lote_id}_{nome_arquivo}", use_container_width=True)
        with col_b3:
            clicou_todos = st.button("🔁 Substituir todos", key=f"btn_todos_{lote_id}_{nome_arquivo}", use_container_width=True)

        if clicou_localizar:
            if not termo_localizar:
                st.warning("Informe o texto a localizar.")
            else:
                ocorrencias = st.session_state[editor_key].count(termo_localizar)
                st.session_state[proximo_idx_key] = 0
                st.info(f"🔍 **{ocorrencias}** ocorrência(s) encontrada(s) para '{termo_localizar}'.")

        if clicou_proximo:
            if not termo_localizar:
                st.warning("Informe o texto a localizar.")
            else:
                texto_atual_edicao = st.session_state[editor_key]
                pos = texto_atual_edicao.find(termo_localizar, st.session_state[proximo_idx_key])
                if pos == -1:
                    pos = texto_atual_edicao.find(termo_localizar)  # tenta novamente a partir do início
                if pos == -1:
                    st.warning(f"Nenhuma ocorrência de '{termo_localizar}' encontrada.")
                else:
                    novo_texto = texto_atual_edicao[:pos] + termo_substituir + texto_atual_edicao[pos + len(termo_localizar):]
                    st.session_state[editor_key] = novo_texto
                    st.session_state[proximo_idx_key] = pos + len(termo_substituir)
                    st.success(f"1 ocorrência substituída (posição {pos}).")

        if clicou_todos:
            if not termo_localizar:
                st.warning("Informe o texto a localizar.")
            else:
                texto_atual_edicao = st.session_state[editor_key]
                qtd = texto_atual_edicao.count(termo_localizar)
                st.session_state[editor_key] = texto_atual_edicao.replace(termo_localizar, termo_substituir)
                st.session_state[proximo_idx_key] = 0
                st.success(f"✅ {qtd} ocorrência(s) substituída(s).")

        # Lê o conteúdo mais atual (já refletindo qualquer substituição feita acima)
        texto_atual = st.session_state[editor_key]
        alterado = texto_atual != st.session_state[baseline_key]

        # A seção "Salvar Alterações" precisa ter sua lógica executada ANTES de
        # editor_placeholder.text_area() ser instanciado (mesma razão do
        # Localizar/Substituir acima) — senão o Streamlit bloqueia a atualização
        # do editor com "StreamlitWidgetAlreadyInstantiatedError" quando o Salvar
        # tenta refletir o novo hash no texto do editor. Container reservado aqui
        # para a seção aparecer, visualmente, DEPOIS do editor e do Localizar/Substituir.
        save_container = st.container()
        with save_container:
            st.divider()
            st.markdown("#### 💾 Salvar Alterações")

            col_status, col_hash = st.columns(2)
            with col_status:
                if alterado:
                    st.warning("🟡 Alterações não salvas")
                elif st.session_state[ja_salvou_key]:
                    st.success("🟢 Alterações salvas")
                else:
                    st.info("🟢 Arquivo original (sem alterações)")

            with col_hash:
                st.caption(f"**Hash original:** `{st.session_state[hash_original_key] or '—'}`")
                st.caption(f"**Hash atual:** `{st.session_state[hash_atual_key] or '—'}`")

            if st.session_state[erro_validacao_key]:
                st.error(f"❌ {st.session_state[erro_validacao_key]}")

            clicou_salvar = st.button(
                "💾 Salvar alterações", type="primary", use_container_width=True,
                disabled=not alterado, key=f"btn_salvar_{lote_id}_{nome_arquivo}"
            )

        if clicou_salvar:
            novos_bytes, erro = validar_e_recalcular_xml_editado(texto_atual)
            if erro:
                st.session_state[erro_validacao_key] = erro
                st.rerun()
            else:
                st.session_state[erro_validacao_key] = None
                novo_texto_final = novos_bytes.decode('ISO-8859-1')
                st.session_state[editor_key] = novo_texto_final
                st.session_state[baseline_key] = novo_texto_final
                st.session_state[hash_atual_key] = _extrair_hash_do_texto(novo_texto_final)
                st.session_state[ja_salvou_key] = True
                resultado['xml_bytes'] = novos_bytes
                st.rerun()

        # Só agora o editor é de fato desenhado (mas aparece visualmente no
        # topo, no espaço reservado pelo placeholder criado antes do
        # Localizar/Substituir) — já reflete qualquer alteração feita pelo
        # Localizar/Substituir ou pelo Salvar, nesta mesma execução.
        # Altura bem maior (era 480px) e resize liberado via CSS para o usuário
        # ajustar o tamanho arrastando o cantinho da caixa.
        editor_placeholder.text_area(
            "Conteúdo do XML",
            key=editor_key,
            height=750,
            label_visibility="collapsed"
        )

        st.divider()
        st.markdown("#### 📎 Ações Secundárias")

        xml_bytes_atuais = resultado['xml_bytes']

        cA, cB = st.columns(2, vertical_alignment="bottom")
        with cA:
            st.download_button(
                label="📥 Baixar XML",
                data=xml_bytes_atuais,
                file_name=f"PRONTO_{nome_arquivo}",
                mime="application/xml",
                use_container_width=True,
                key=f"dl_{lote_id}_{nome_arquivo}"
            )
        with cB:
            botao_copiar_codigo(st.session_state[editor_key], key_sufixo=f"copia_{lote_id}_{nome_arquivo}")

        with st.expander("📝 Ver Detalhes das Modificações Automáticas"):
            tem_alteracao = False
            if isinstance(aud, dict):
                for chave, lista_logs in aud.items():
                    if lista_logs:
                        tem_alteracao = True
                        st.markdown(f"**{TITULOS_AMIGAVEIS_AUDITORIA.get(chave, chave)}**")
                        for item in lista_logs:
                            st.caption(f"• {item}")
                        st.markdown("---")
            if not tem_alteracao:
                st.info("Nenhuma alteração foi necessária neste XML.")


st.markdown("<br>", unsafe_allow_html=True)

with st.container(border=True):
    st.markdown("### 🛠️ Parametrização e Regras de Negócio")
    
    abas = st.tabs([
        "🔄 Equipe SADT (Nova)",
        "👩‍⚕️ CBO e Cód Operadora", 
        "⚙️ Procedimentos", 
        "🤝 Médicos Conveniados", 
        "🛡️ Blindagem", 
        "💊 Itens e Meds", 
        "📦 Unidades", 
        "🏥 Registro ANVISA"
    ])

    tabelas_nomes = ['troca_equipe_sadt', 'medicos', 'procedimentos', 'conveniados', 'blindagem', 'itens', 'unidades', 'anvisa']
    
    for i, aba_nome in enumerate(tabelas_nomes):
        with abas[i]:
            st.session_state[f'tab_{aba_nome}'] = st.data_editor(
                st.session_state[f'tab_{aba_nome}'], 
                num_rows="dynamic", 
                use_container_width=True, 
                column_config=config_texto_colunas
            )
