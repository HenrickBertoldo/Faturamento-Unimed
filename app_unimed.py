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
st.set_page_config(page_title="Validador TISS", layout="wide", page_icon="📄")

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
                        if grau_val:
                            for eq in equipes_restantes:
                                target_node = eq if tag_limpa(eq) == 'equipeSadt' else (eq.find('ans:identificacaoEquipe', NS) or eq)
                                grau_elem = target_node.find('ans:grauPart', NS)
                                if grau_elem is not None: grau_elem.text = grau_val
                                else:
                                    grau_elem = ET.Element(ans_tag('grauPart'))
                                    grau_elem.text = grau_val
                                    target_node.insert(0, grau_elem)
                                
                                for parent in eq.iter():
                                    for bad_grau in parent.findall('ans:grauParticipacao', NS): parent.remove(bad_grau)
                                        
                            detalhes_proc.append(f"Grau inserido: {grau_val}")
                            
                        quantidade_elem = proc_exec.find('ans:quantidadeExecutada', NS)
                        indent_tail = quantidade_elem.tail if quantidade_elem is not None else None

                        def _normaliza_via_tecnica(valor):
                            return str(int(valor)) if valor.isdigit() else valor

                        via_val = str(regra_p.get('Via de Acesso (1, 2 ou EXCLUIR)', '')).strip().upper()
                        via_elem = proc_exec.find('ans:viaAcesso', NS)
                        if via_val == 'EXCLUIR' and via_elem is not None:
                            proc_exec.remove(via_elem)
                            via_elem = None
                            detalhes_proc.append("Via de Acesso excluída")
                        elif via_val in ['1', '2', '01', '02']:
                            via_val = _normaliza_via_tecnica(via_val)
                            if via_elem is not None: via_elem.text = via_val
                            else:
                                via_elem = ET.Element(ans_tag('viaAcesso'))
                                via_elem.text = via_val
                                via_elem.tail = indent_tail
                                proc_exec.insert(indice_apos(proc_exec, quantidade_elem), via_elem)
                            detalhes_proc.append(f"Via de Acesso ajustada: {via_val}")
                            
                        tec_val = str(regra_p.get('Técnica (1, 2 ou EXCLUIR)', '')).strip().upper()
                        tec_elem = proc_exec.find('ans:tecnicaUtilizada', NS)
                        if tec_val == 'EXCLUIR' and tec_elem is not None:
                            proc_exec.remove(tec_elem)
                            detalhes_proc.append("Técnica excluída")
                        elif tec_val in ['1', '2', '01', '02']:
                            tec_val = _normaliza_via_tecnica(tec_val)
                            if tec_elem is not None: tec_elem.text = tec_val
                            else:
                                tec_elem = ET.Element(ans_tag('tecnicaUtilizada'))
                                tec_elem.text = tec_val
                                tec_elem.tail = indent_tail
                                ref_apos = via_elem if via_elem is not None else quantidade_elem
                                proc_exec.insert(indice_apos(proc_exec, ref_apos), tec_elem)
                            detalhes_proc.append(f"Técnica ajustada: {tec_val}")
                            
                        if detalhes_proc: auditoria['procedimentos_ajustados'].append(f"Proc {cod_p}: " + " | ".join(detalhes_proc))

                    # AJUSTES DE CBO E CÓDIGO OPERADORA DOS MÉDICOS
                    for eq in equipes_restantes:
                        nome_prof_elem = eq.find('.//ans:nomeProf', NS)
                        nome_prof = nome_prof_elem.text.strip().upper() if nome_prof_elem is not None and nome_prof_elem.text else ""
                        
                        if nome_prof in set_conveniados: continue 
                        
                        if nome_prof in dict_medicos:
                            regra_m = dict_medicos[nome_prof]
                            cbo_novo = limpar_numero(regra_m.get('CBO Correto', ''))
                            
                            target_node = eq if tag_limpa(eq) == 'equipeSadt' else (eq.find('ans:identificacaoEquipe', NS) or eq)

                            # Busca qualquer tag de CBO existente na estrutura
                            cbos_existentes = [elem for elem in eq.iter() if tag_limpa(elem) in ['CBOS', 'codigoCBOS', 'codigoCBO']]

                            if cbo_novo != '':
                                if cbos_existentes:
                                    # Atualiza o CBO original diretamente
                                    primeiro_cbo = cbos_existentes[0]
                                    if primeiro_cbo.text != cbo_novo:
                                        primeiro_cbo.text = cbo_novo
                                        auditoria['cbos'].append(f"Médico(a) '{nome_prof}': CBO alterado para {cbo_novo}")
                                    
                                    # Se houver duplicatas por erro antigo, remove as extras
                                    for c_extra in cbos_existentes[1:]:
                                        for parent in eq.iter():
                                            if c_extra in list(parent): parent.remove(c_extra)
                                else:
                                    # Insere o novo CBO dentro do nó correto (identificacaoEquipe / equipeSadt)
                                    novo_cbo = ET.Element(ans_tag('CBOS'))
                                    novo_cbo.text = cbo_novo
                                    target_node.append(novo_cbo)
                                    auditoria['cbos'].append(f"Médico(a) '{nome_prof}': CBO inserido ({cbo_novo})")
                            
                            substituir = str(regra_m.get('Substituir por Cód. Operadora', '')).strip().upper() == 'SIM'
                            cod_operadora = limpar_numero(regra_m.get('Código na Operadora', ''))
                            
                            if substituir and cod_operadora != '':
                                cod_prof_elem = eq.find('.//ans:codProfissional', NS)
                                if cod_prof_elem is not None:
                                    cpf_elem = cod_prof_elem.find('ans:cpfContratado', NS)
                                    cod_op_elem = cod_prof_elem.find('ans:codigoPrestadorNaOperadora', NS)
                                    if cpf_elem is not None:
                                        cpf_elem.tag = ans_tag('codigoPrestadorNaOperadora')
                                        cpf_elem.text = cod_operadora
                                        auditoria['cbos'].append(f"Médico(a) '{nome_prof}': CPF -> Cód. Operadora {cod_operadora}")
                                    elif cod_op_elem is not None:
                                        cod_op_elem.text = cod_operadora
                                        auditoria['cbos'].append(f"Médico(a) '{nome_prof}': Cód. Operadora alterado para {cod_operadora}")

                for p in procs_para_remover: procs_container.remove(p)

                # 🆕 NOVA REGRA: escalona horários de procedimentos duplicados (mesmo
                # código + data + horário) para evitar a crítica "Serviço duplicado".
                ajustar_horarios_duplicados(procs_container, auditoria)

            # --- OUTRAS DESPESAS ---
            despesas_container = guia.find('.//ans:outrasDespesas', NS)
            if despesas_container is not None:
                for despesa in despesas_container.findall('ans:despesa', NS):
                    servicos = despesa.find('ans:servicosExecutados', NS)
                    if servicos is not None:
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
# INTERFACE GRÁFICA — EDITOR TISS
# ==========================================
import difflib
from html import escape

# ---------- CSS: aparência de editor desktop ----------
st.markdown("""
<style>
/* Aproveitar melhor a tela */
.block-container {
    padding-top: 0.65rem !important;
    padding-bottom: 1rem !important;
    max-width: 100% !important;
}
#MainMenu {visibility: hidden;}
footer {visibility: hidden;}
header {visibility: hidden;}

.app-topbar {
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:16px;
    padding:7px 10px;
    border-bottom:1px solid #d8dde5;
    background:#f7f8fa;
    margin-bottom:6px;
}
.app-brand {
    display:flex;
    align-items:center;
    gap:9px;
    font-size:17px;
    font-weight:650;
    color:#20242a;
}
.app-file {
    font-family:Consolas, "Courier New", monospace;
    font-size:13px;
    color:#555d68;
}
.app-file.modified {
    color:#b26a00;
    font-weight:650;
}
.toolbar-label {
    font-size:11px;
    color:#6b7280;
    margin-bottom:3px;
    text-transform:uppercase;
    letter-spacing:.04em;
}
.statusbar {
    display:flex;
    align-items:center;
    gap:18px;
    min-height:28px;
    padding:3px 9px;
    border:1px solid #d8dde5;
    background:#f7f8fa;
    color:#555d68;
    font-size:11px;
    font-family:Consolas, "Courier New", monospace;
    margin-top:5px;
}
.status-ok { color:#188038; font-weight:650; }
.status-warn { color:#b26a00; font-weight:650; }
.hash-text {
    overflow:hidden;
    text-overflow:ellipsis;
    white-space:nowrap;
    max-width:300px;
}
.change-panel {
    height:720px;
    overflow-y:auto;
    border:1px solid #d8dde5;
    background:#fbfcfd;
    padding:10px;
    font-family:Arial, sans-serif;
    font-size:12px;
}
.change-header {
    position:sticky;
    top:-10px;
    background:#fbfcfd;
    padding:3px 0 8px 0;
    border-bottom:1px solid #e2e6eb;
    margin-bottom:8px;
    z-index:2;
}
.change-title {
    font-size:13px;
    font-weight:700;
    color:#20242a;
}
.change-count {
    color:#6b7280;
    font-size:11px;
    margin-top:2px;
}
.change-section {
    margin-top:10px;
    margin-bottom:4px;
    color:#4b5563;
    font-weight:700;
    font-size:11px;
    text-transform:uppercase;
    letter-spacing:.03em;
}
.change-item {
    border-left:3px solid #d97706;
    background:#fffaf0;
    padding:6px 7px;
    margin:5px 0;
    border-radius:2px;
}
.change-item.auto { border-left-color:#2563eb; background:#f4f7ff; }
.change-item.error { border-left-color:#dc2626; background:#fff5f5; }
.change-line {
    color:#7a828d;
    font-family:Consolas, "Courier New", monospace;
    font-size:10px;
    margin-bottom:3px;
}
.change-old {
    color:#b42318;
    background:#fff0ef;
    padding:2px 4px;
    font-family:Consolas, "Courier New", monospace;
    white-space:pre-wrap;
    word-break:break-word;
}
.change-new {
    color:#137333;
    background:#edf7ed;
    padding:2px 4px;
    margin-top:2px;
    font-family:Consolas, "Courier New", monospace;
    white-space:pre-wrap;
    word-break:break-word;
}
.audit-item {
    color:#374151;
    padding:4px 0;
    border-bottom:1px solid #eef0f2;
}
.empty-changes {
    color:#7a828d;
    text-align:center;
    padding:35px 10px;
}
div[data-testid="stTextArea"] textarea {
    font-family:Consolas, "Courier New", monospace !important;
    font-size:13px !important;
    line-height:1.42 !important;
    tab-size:4 !important;
    resize:vertical !important;
    border-radius:2px !important;
    border:1px solid #aeb6c2 !important;
    background:#ffffff !important;
}
div[data-testid="stTextArea"] label { display:none; }
div[data-testid="stFileUploader"] {
    padding-top:0 !important;
}
div[data-testid="stButton"] > button {
    border-radius:3px !important;
    min-height:32px !important;
}
div[data-testid="stDownloadButton"] > button {
    border-radius:3px !important;
}
.editor-caption {
    color:#6b7280;
    font-size:11px;
    margin:2px 0 5px 0;
}
</style>
""", unsafe_allow_html=True)

# ---------- Funções auxiliares da interface ----------
def contar_auditoria(auditoria):
    if not isinstance(auditoria, dict):
        return 0
    return sum(len(v) for v in auditoria.values() if isinstance(v, list))

def diffs_manuais(baseline, atual, limite=120):
    """Gera uma lista compacta de alterações manuais, comparando linhas."""
    if baseline == atual:
        return []

    antigas = baseline.splitlines()
    novas = atual.splitlines()
    sm = difflib.SequenceMatcher(None, antigas, novas)
    alteracoes = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue

        linha = j1 + 1 if j1 < len(novas) else max(1, len(novas))
        antigas_bloco = antigas[i1:i2]
        novas_bloco = novas[j1:j2]

        # Uma alteração pode envolver várias linhas. Mantemos o painel compacto.
        max_itens = max(len(antigas_bloco), len(novas_bloco), 1)
        for k in range(max_itens):
            velho = antigas_bloco[k] if k < len(antigas_bloco) else ""
            novo = novas_bloco[k] if k < len(novas_bloco) else ""
            alteracoes.append({
                "linha": linha + k,
                "velho": velho,
                "novo": novo,
                "tipo": tag
            })
            if len(alteracoes) >= limite:
                return alteracoes
    return alteracoes

def html_painel_alteracoes(auditoria, manuais, erro=None):
    total_auto = contar_auditoria(auditoria)
    total_manual = len(manuais)
    total = total_auto + total_manual

    html = [
        '<div class="change-panel">',
        '<div class="change-header">',
        '<div class="change-title">Alterações</div>',
        f'<div class="change-count">{total} registro(s) · {total_auto} automáticas · {total_manual} manuais</div>',
        '</div>'
    ]

    if erro:
        html.append('<div class="change-section">Validação</div>')
        html.append(f'<div class="change-item error"><b>Erro ao salvar</b><br>{escape(erro)}</div>')

    if manuais:
        html.append('<div class="change-section">Edições no editor</div>')
        for item in manuais:
            html.append('<div class="change-item">')
            html.append(f'<div class="change-line">Linha {item["linha"]}</div>')
            if item["velho"]:
                html.append(f'<div class="change-old">− {escape(item["velho"])}</div>')
            if item["novo"]:
                html.append(f'<div class="change-new">+ {escape(item["novo"])}</div>')
            html.append('</div>')

        if len(manuais) >= 120:
            html.append('<div class="change-count">Exibindo as primeiras 120 alterações manuais.</div>')

    if isinstance(auditoria, dict):
        tem_auto = False
        for chave, lista_logs in auditoria.items():
            if not lista_logs:
                continue
            tem_auto = True
            titulo = TITULOS_AMIGAVEIS_AUDITORIA.get(chave, chave)
            html.append(f'<div class="change-section">{escape(titulo)}</div>')
            for item in lista_logs:
                html.append(f'<div class="audit-item">• {escape(str(item))}</div>')

        if not tem_auto and not manuais and not erro:
            html.append('<div class="empty-changes">Nenhuma alteração realizada.</div>')
    elif not manuais and not erro:
        html.append('<div class="empty-changes">Nenhuma alteração realizada.</div>')

    html.append('</div>')
    return ''.join(html)


TITULOS_AMIGAVEIS_AUDITORIA = {
    'medicos_trocados': 'Médicos e CRMs substituídos',
    'cbos': 'Médicos e CBOs alterados',
    'itens': 'Itens e medicamentos traduzidos',
    'anvisa': 'Registros ANVISA inseridos',
    'unidades': 'Unidades de medida ajustadas',
    'oxigenio': 'Tempos de oxigênio recalculados',
    'conveniados_excluidos': 'Médicos conveniados removidos',
    'procedimentos_ajustados': 'Procedimentos ajustados (grau/via/técnica)',
    'guias_blindadas': 'Guias blindadas',
    'erros': 'Avisos e erros durante o processamento',
    'valores_negativos': 'Valores negativos corrigidos',
    'motivo_encerramento': 'Motivo de encerramento (11 → 12)',
    'horarios_duplicados': 'Horários escalonados (anti-duplicidade)'
}

def botao_copiar_codigo(xml_str, key_sufixo):
    texto_escaped = (
        xml_str.replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("${", "\\${")
    )
    html_copiar = f"""
    <button id="cpBtn_{key_sufixo}" style="
        width:100%; background:#FFFFFF; color:#1E1E1E;
        border:1px solid #CCCCCC; padding:8px; border-radius:3px;
        cursor:pointer; font-size:13px; font-weight:600;
    ">📋 Copiar XML</button>
    <script>
    document.getElementById("cpBtn_{key_sufixo}").addEventListener("click", () => {{
        navigator.clipboard.writeText(`{texto_escaped}`).then(() => {{
            const b = document.getElementById("cpBtn_{key_sufixo}");
            b.innerText = "✓ Copiado";
            setTimeout(() => b.innerText = "📋 Copiar XML", 2000);
        }});
    }});
    </script>
    """
    components.html(html_copiar, height=42)

# ---------- Inicialização das tabelas ----------
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

# ---------- Cabeçalho ----------
tem_resultados = bool(st.session_state.get('resultados_lote'))
resultado = None
if tem_resultados:
    resultados = st.session_state['resultados_lote']
    nome_selecionado = st.session_state.get('arquivo_selecionado')
    if not nome_selecionado or nome_selecionado not in [r['nome'] for r in resultados]:
        nome_selecionado = resultados[0]['nome']
        st.session_state['arquivo_selecionado'] = nome_selecionado
    resultado = next(r for r in resultados if r['nome'] == nome_selecionado)

nome_cabecalho = resultado['nome'] if resultado else "Nenhum XML aberto"
lote_id_global = st.session_state.get('lote_id', 0)
modified_marker = ""

if resultado and not resultado.get('falha_total'):
    _tmp_editor_key = f"editor_texto_{lote_id_global}_{resultado['nome']}"
    _tmp_baseline_key = f"editor_baseline_{lote_id_global}_{resultado['nome']}"
    if _tmp_editor_key in st.session_state and _tmp_baseline_key in st.session_state:
        if st.session_state[_tmp_editor_key] != st.session_state[_tmp_baseline_key]:
            modified_marker = " *"

st.markdown(
    f'<div class="app-topbar">'
    f'<div class="app-brand">📄 Validador TISS <span class="app-file {"modified" if modified_marker else ""}">— {escape(nome_cabecalho)}{modified_marker}</span></div>'
    f'<div class="app-file">Editor XML · TISS / UNIMED</div>'
    f'</div>',
    unsafe_allow_html=True
)

# ---------- Barra de abertura/processamento ----------
with st.container():
    c_upload, c_action, c_file, c_sync = st.columns([2.2, 1.35, 2.1, 1.35], gap="small")

    with c_upload:
        xml_up = st.file_uploader(
            "Abrir XML",
            type=['xml'],
            accept_multiple_files=True,
            label_visibility="collapsed",
            key="xml_upload_editor"
        )

    with c_action:
        st.markdown('<div class="toolbar-label">Processamento</div>', unsafe_allow_html=True)
        iniciar = st.button(
            "▶ Processar XML",
            type="primary",
            use_container_width=True,
            disabled=not xml_up,
            key="btn_processar_editor"
        )

    with c_file:
        if tem_resultados and len(resultados) > 1:
            opcoes = [r['nome'] for r in resultados]
            indice_atual = opcoes.index(st.session_state['arquivo_selecionado'])
            escolhido = st.selectbox(
                "Arquivo",
                opcoes,
                index=indice_atual,
                label_visibility="collapsed",
                key="arquivo_seletor_ui"
            )
            if escolhido != st.session_state['arquivo_selecionado']:
                st.session_state['arquivo_selecionado'] = escolhido
                st.rerun()
        else:
            st.caption("Abra um ou mais XML para começar.")

    with c_sync:
        if st.button("☁ Sincronizar regras", use_container_width=True, key="btn_sync_editor"):
            carregar_do_sheets()
            st.rerun()

# ---------- Processamento do lote ----------
if iniciar and xml_up:
    dfs_atuais = {k: st.session_state[f'tab_{k}'] for k in tabelas_padrao.keys()}
    resultados_lote = []
    barra = st.progress(0.0, text="Processando arquivos...")

    for i, arquivo in enumerate(xml_up):
        resultado_lote = {'nome': arquivo.name, 'xml_bytes': None, 'auditoria': None, 'falha_total': None}
        try:
            xml_resultado, auditoria = processar_xml_tiss(arquivo, dfs_atuais)
            resultado_lote['xml_bytes'] = xml_resultado
            resultado_lote['auditoria'] = auditoria
        except Exception as e:
            resultado_lote['falha_total'] = str(e)
        resultados_lote.append(resultado_lote)
        barra.progress((i + 1) / len(xml_up), text=f"Processando... ({i+1}/{len(xml_up)})")

    barra.empty()
    st.session_state['resultados_lote'] = resultados_lote
    st.session_state['lote_id'] = st.session_state.get('lote_id', 0) + 1
    st.session_state['arquivo_selecionado'] = resultados_lote[0]['nome']
    st.rerun()

# ---------- Recalcular estado após possível processamento ----------
tem_resultados = bool(st.session_state.get('resultados_lote'))
if tem_resultados:
    resultados = st.session_state['resultados_lote']
    nome_selecionado = st.session_state.get('arquivo_selecionado', resultados[0]['nome'])
    if nome_selecionado not in [r['nome'] for r in resultados]:
        nome_selecionado = resultados[0]['nome']
        st.session_state['arquivo_selecionado'] = nome_selecionado
    resultado = next(r for r in resultados if r['nome'] == nome_selecionado)

# ---------- Editor ----------
if tem_resultados and resultado and not resultado.get('falha_total'):
    nome_arquivo = resultado['nome']
    lote_id = st.session_state.get('lote_id', 0)

    editor_key = f"editor_texto_{lote_id}_{nome_arquivo}"
    baseline_key = f"editor_baseline_{lote_id}_{nome_arquivo}"
    hash_original_key = f"hash_original_{lote_id}_{nome_arquivo}"
    hash_atual_key = f"hash_atual_{lote_id}_{nome_arquivo}"
    ja_salvou_key = f"ja_salvou_{lote_id}_{nome_arquivo}"
    proximo_idx_key = f"proximo_idx_{lote_id}_{nome_arquivo}"
    erro_validacao_key = f"erro_validacao_{lote_id}_{nome_arquivo}"

    if editor_key not in st.session_state:
        xml_texto = resultado['xml_bytes'].decode('ISO-8859-1')
        st.session_state[editor_key] = xml_texto
        st.session_state[baseline_key] = xml_texto
        st.session_state[hash_original_key] = _extrair_hash_do_texto(xml_texto)
        st.session_state[hash_atual_key] = st.session_state[hash_original_key]
        st.session_state[ja_salvou_key] = False
        st.session_state[proximo_idx_key] = 0
        st.session_state[erro_validacao_key] = None

    # Estado atual antes de desenhar os widgets
    texto_atual = st.session_state[editor_key]
    baseline = st.session_state[baseline_key]
    alterado = texto_atual != baseline
    manual_changes = diffs_manuais(baseline, texto_atual)

    # ---------- Toolbar de edição ----------
    b1, b2, b3, b4, b5, b6 = st.columns([1.0, 1.0, 1.0, 1.0, 1.35, 1.35], gap="small")

    with b1:
        clicou_localizar = st.button("🔎 Localizar", use_container_width=True, key=f"tb_find_{lote_id}_{nome_arquivo}")
    with b2:
        clicou_substituir = st.button("⇄ Substituir", use_container_width=True, key=f"tb_replace_{lote_id}_{nome_arquivo}")
    with b3:
        clicou_validar = st.button("✓ Validar", use_container_width=True, key=f"tb_validate_{lote_id}_{nome_arquivo}")
    with b4:
        clicou_salvar = st.button("💾 Salvar", type="primary", use_container_width=True,
                                  disabled=not alterado, key=f"tb_save_{lote_id}_{nome_arquivo}")
    with b5:
        st.download_button(
            "📥 Baixar XML",
            data=resultado['xml_bytes'],
            file_name=f"PRONTO_{nome_arquivo}",
            mime="application/xml",
            use_container_width=True,
            key=f"tb_download_{lote_id}_{nome_arquivo}"
        )
    with b6:
        st.caption(f"**{len(manual_changes)}** alteração(ões) manual(is)" if manual_changes else "Sem alterações manuais")

    # ---------- Localizar / substituir ----------
    if clicou_localizar or clicou_substituir or st.session_state.get(f"mostrar_busca_{lote_id}_{nome_arquivo}", False):
        st.session_state[f"mostrar_busca_{lote_id}_{nome_arquivo}"] = True
        with st.container(border=True):
            f1, f2, f3, f4 = st.columns([2.3, 2.3, 1.0, 1.0], gap="small")
            with f1:
                termo_localizar = st.text_input(
                    "Localizar",
                    key=f"loc_localizar_{lote_id}_{nome_arquivo}",
                    placeholder="Texto, tag ou valor..."
                )
            with f2:
                termo_substituir = st.text_input(
                    "Substituir por",
                    key=f"loc_substituir_{lote_id}_{nome_arquivo}",
                    placeholder="Novo valor..."
                )
            with f3:
                acao_find = st.button("Encontrar", use_container_width=True, key=f"find_now_{lote_id}_{nome_arquivo}")
            with f4:
                acao_replace = st.button("Substituir todos", use_container_width=True, key=f"replace_now_{lote_id}_{nome_arquivo}")

            if acao_find:
                if not termo_localizar:
                    st.warning("Informe o texto a localizar.")
                else:
                    ocorrencias = st.session_state[editor_key].count(termo_localizar)
                    st.session_state[proximo_idx_key] = 0
                    st.info(f"🔎 {ocorrencias} ocorrência(s) encontrada(s).")

            if acao_replace:
                if not termo_localizar:
                    st.warning("Informe o texto a localizar.")
                else:
                    texto_edicao = st.session_state[editor_key]
                    qtd = texto_edicao.count(termo_localizar)
                    st.session_state[editor_key] = texto_edicao.replace(termo_localizar, termo_substituir)
                    st.session_state[proximo_idx_key] = 0
                    st.rerun()

    # ---------- Salvar / validar ANTES do editor ----------
    if clicou_validar:
        novos_bytes, erro = validar_e_recalcular_xml_editado(st.session_state[editor_key])
        if erro:
            st.session_state[erro_validacao_key] = erro
        else:
            st.session_state[erro_validacao_key] = None
            st.toast("✓ XML válido e hash recalculado.", icon="✅")

    if clicou_salvar:
        novos_bytes, erro = validar_e_recalcular_xml_editado(st.session_state[editor_key])
        if erro:
            st.session_state[erro_validacao_key] = erro
        else:
            st.session_state[erro_validacao_key] = None
            novo_texto_final = novos_bytes.decode('ISO-8859-1')
            st.session_state[editor_key] = novo_texto_final
            st.session_state[baseline_key] = novo_texto_final
            st.session_state[hash_atual_key] = _extrair_hash_do_texto(novo_texto_final)
            st.session_state[ja_salvou_key] = True
            resultado['xml_bytes'] = novos_bytes
            st.toast("✓ Alterações salvas e hash atualizado.", icon="💾")
            st.rerun()

    # Estado depois das ações de toolbar
    texto_atual = st.session_state[editor_key]
    alterado = texto_atual != st.session_state[baseline_key]
    manual_changes = diffs_manuais(st.session_state[baseline_key], texto_atual)

    # ---------- Layout principal: editor + alterações fixas ----------
    col_editor, col_changes = st.columns([4.2, 1.15], gap="small")

    with col_editor:
        status = "ALTERAÇÕES NÃO SALVAS" if alterado else ("SALVO" if st.session_state[ja_salvou_key] else "ORIGINAL")
        status_class = "status-warn" if alterado else "status-ok"

        st.markdown(
            f'<div class="editor-caption"><b>{escape(nome_arquivo)}</b> · '
            f'<span class="{status_class}">{status}</span> · '
            f'{len(texto_atual.splitlines()):,} linhas</div>',
            unsafe_allow_html=True
        )

        st.text_area(
            "Conteúdo XML",
            key=editor_key,
            height=720,
            label_visibility="collapsed"
        )

        hash_original = st.session_state.get(hash_original_key) or "—"
        hash_atual = st.session_state.get(hash_atual_key) or "—"
        xml_valido = "XML válido" if not st.session_state.get(erro_validacao_key) else "XML com erro"

        st.markdown(
            f'<div class="statusbar">'
            f'<span class="{"status-ok" if xml_valido == "XML válido" else "status-warn"}">● {xml_valido}</span>'
            f'<span>{len(texto_atual.splitlines()):,} linhas</span>'
            f'<span>{"● Alterado" if alterado else "● Sem alterações"}</span>'
            f'<span class="hash-text">Hash original: {escape(hash_original)}</span>'
            f'<span class="hash-text">Hash atual: {escape(hash_atual)}</span>'
            f'</div>',
            unsafe_allow_html=True
        )

        if st.session_state.get(erro_validacao_key):
            st.error(st.session_state[erro_validacao_key])

    with col_changes:
        st.markdown(
            html_painel_alteracoes(
                resultado.get('auditoria', {}),
                manual_changes,
                st.session_state.get(erro_validacao_key)
            ),
            unsafe_allow_html=True
        )

    # ---------- Ações rápidas ----------
    q1, q2, q3 = st.columns([1.0, 1.0, 2.2], gap="small")
    with q1:
        if st.button("↶ Restaurar original", use_container_width=True, key=f"restore_{lote_id}_{nome_arquivo}"):
            st.session_state[editor_key] = st.session_state[baseline_key]
            st.session_state[erro_validacao_key] = None
            st.rerun()
    with q2:
        if st.button("📋 Copiar XML", use_container_width=True, key=f"copy_btn_{lote_id}_{nome_arquivo}"):
            botao_copiar_codigo(st.session_state[editor_key], key_sufixo=f"copy_{lote_id}_{nome_arquivo}")
    with q3:
        if alterado:
            st.caption("As alterações do editor só passam a compor o arquivo final depois de **Salvar**.")
        else:
            st.caption("O painel à direita permanece visível para acompanhar as alterações automáticas e manuais.")

elif tem_resultados and resultado and resultado.get('falha_total'):
    st.error(f"❌ {resultado['nome']}: {resultado['falha_total']}")

else:
    # Estado inicial — pequeno e sem ocupar a tela toda
    with st.container(border=True):
        st.markdown("### 📂 Nenhum XML aberto")
        st.caption("Selecione um ou mais arquivos XML acima e clique em **Processar XML**.")
        st.info("Depois do processamento, o editor ocupará a tela principal e o histórico de alterações ficará fixo ao lado.")

# ---------- Parametrização (mantida, mas recolhida) ----------
with st.expander("🛠️ Parametrização e Regras de Negócio", expanded=False):
    st.caption("Área administrativa da aplicação. Nenhuma instalação adicional é necessária para usar o editor.")
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

    tabelas_nomes = [
        'troca_equipe_sadt', 'medicos', 'procedimentos', 'conveniados',
        'blindagem', 'itens', 'unidades', 'anvisa'
    ]

    for i, aba_nome in enumerate(tabelas_nomes):
        with abas[i]:
            st.session_state[f'tab_{aba_nome}'] = st.data_editor(
                st.session_state[f'tab_{aba_nome}'],
                num_rows="dynamic",
                use_container_width=True,
                column_config=config_texto_colunas
            )
