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
    """Retorna o índice, dentro de 'parent', imediatamente após 'elem_ref'.
    Se elem_ref for None, retorna o final da lista de filhos (append)."""
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
        
        # 🌟 CORREÇÃO: Força 2 dígitos com zero à esquerda para colunas específicas
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
    """Retorna (hora_calculada, sucesso). Em caso de erro de formato, devolve
    a hora original e sucesso=False para que o chamador registre a falha na
    auditoria em vez de mascará-la silenciosamente."""
    try:
        t_ini = datetime.strptime(hora_ini_str.strip(), "%H:%M:%S")
        qtd = float(qtd_executada.strip())
        if tipo_unidade == '60034335': return (t_ini + timedelta(hours=qtd)).strftime("%H:%M:%S"), True
        elif tipo_unidade == '60034343': return (t_ini + timedelta(minutes=qtd)).strftime("%H:%M:%S"), True
        return hora_ini_str, True
    except (ValueError, AttributeError, TypeError):
        return hora_ini_str, False

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
    auditoria = { 
        'cbos': [], 'medicos_trocados': [], 'itens': [], 'anvisa': [], 'unidades': [], 'oxigenio': [],
        'conveniados_excluidos': [], 'procedimentos_ajustados': [], 'guias_blindadas': [], 'erros': []
    }
    tree = ET.parse(arquivo_xml)
    root = tree.getroot()
    
    dict_medicos = {str(r['Nome do Médico']).strip().upper(): r for _, r in dfs['medicos'].iterrows()}
    
    # 🔄 MAPEAMENTO EXCLUSIVO PARA TROCA DE EQUIPE COMPLETA EM SADT
    dict_equipe_sadt = {}
    if 'troca_equipe_sadt' in dfs:
        for _, r in dfs['troca_equipe_sadt'].iterrows():
            orig = str(r['Nome Original (Erro)']).strip().upper()
            if orig and orig != 'NAN' and orig != '':
                dict_equipe_sadt[orig] = {
                    'nome_novo': str(r.get('Nome Novo', '')).strip(),
                    'crm_novo': limpar_numero(r.get('CRM Novo', '')),
                    'cbo_novo': limpar_numero(r.get('CBO Novo', '')),
                    'cod_op_novo': limpar_numero(r.get('Cód Operadora Novo', '')),
                    'grau_novo': limpar_numero(r.get('Grau Part Novo', '')),
                    'conselho_novo': limpar_numero(r.get('Conselho Novo', '')),
                    'uf_nova': limpar_numero(r.get('UF Nova', ''))
                }

    set_conveniados = set(dfs['conveniados']['Nome do Médico Conveniado'].str.strip().str.upper().dropna())
    set_blindagem = set(dfs['blindagem']['Código Prestador Protegido'].apply(limpar_numero).dropna())
    dict_itens = {padronizar_codigo_8_digitos(k): padronizar_codigo_8_digitos(v) for k, v in zip(dfs['itens']['Código Incorreto'], dfs['itens']['Código Correto']) if pd.notna(k)}
    dict_unidades = {padronizar_codigo_8_digitos(r['Código do Item']): limpar_numero(r['Unidade de Medida Correta']) for _, r in dfs['unidades'].iterrows() if pd.notna(r['Código do Item'])}
    dict_anvisa = {padronizar_codigo_8_digitos(r['Código do Item']): r for _, r in dfs['anvisa'].iterrows() if pd.notna(r['Código do Item'])}
    dict_procedimentos = {padronizar_codigo_8_digitos(r['Código do Procedimento']): r for _, r in dfs['procedimentos'].iterrows() if pd.notna(r['Código do Procedimento'])}

    guias_int = [(g, 'internacao') for g in root.findall('.//ans:guiaResumoInternacao', NS)]
    guias_sadt = [(g, 'sadt') for g in root.findall('.//ans:guiaSP-SADT', NS)]
    todas_guias = guias_int + guias_sadt

    for indice_guia, (guia, tipo_guia) in enumerate(todas_guias, start=1):
      try:
        # 🛠️ CORREÇÃO: 'or' entre Elements do ElementTree é perigoso — um elemento
        # sem filhos (como esse, que só tem texto) avalia como False em booleano,
        # então o 'or' sempre pulava para o segundo find(), mesmo quando o
        # primeiro existia e estava correto. Trocado por checagem explícita.
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

        # =========================================================================
        # ⚡ REGRA EXCLUSIVA: SUBSTITUIÇÃO DE EQUIPE EM GUIAS SADT
        # =========================================================================
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
                            cbo_el = eq_sadt.find('ans:CBOS', NS)
                            if cbo_el is not None: cbo_el.text = regra['cbo_novo']
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


        # =========================================================================
        # REGRAS COMPLEMENTARES DE PROCEDIMENTOS, CBOS E REMOÇÕES
        # =========================================================================
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
                
                # Ajustes de GrauPart, Via e Técnica nos procedimentos
                if cod_p in dict_procedimentos:
                    regra_p = dict_procedimentos[cod_p]
                    detalhes_proc = []
                    
                    grau_val = limpar_numero(regra_p.get('Grau Part Obrigatório', ''))
                    if grau_val:
                        for eq in equipes_restantes:
                            if tag_limpa(eq) == 'equipeSadt': target_node = eq
                            else:
                                target_node = eq.find('ans:identificacaoEquipe', NS)
                                if target_node is None: target_node = eq
                                
                            grau_elem = target_node.find('ans:grauPart', NS)
                            if grau_elem is not None: grau_elem.text = grau_val
                            else:
                                grau_elem = ET.Element(ans_tag('grauPart'))
                                grau_elem.text = grau_val
                                target_node.insert(0, grau_elem)
                            
                            for parent in eq.iter():
                                for bad_grau in parent.findall('ans:grauParticipacao', NS): parent.remove(bad_grau)
                                    
                        detalhes_proc.append(f"Grau inserido: {grau_val}")
                        
                    # 🛠️ CORREÇÃO: viaAcesso e tecnicaUtilizada precisam ficar logo após
                    # quantidadeExecutada (ordem exigida pelo schema TISS), nunca no final
                    # de procedimentoExecutado (depois de identEquipe) — era isso que
                    # gerava o erro "elemento viaAcesso não é esperado".
                    quantidade_elem = proc_exec.find('ans:quantidadeExecutada', NS)
                    # 🛠️ Captura o padrão de indentação/quebra de linha usado pelos irmãos
                    # deste nível, para que tags recém-criadas herdem a mesma formatação
                    # em vez de ficarem "grudadas" na mesma linha da tag anterior.
                    indent_tail = quantidade_elem.tail if quantidade_elem is not None else None

                    def _normaliza_via_tecnica(valor):
                        # 🛠️ CORREÇÃO: o schema TISS não aceita zero à esquerda em
                        # viaAcesso/tecnicaUtilizada — "01"/"02" precisam virar "1"/"2".
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
                        
                    # 🛠️ CORREÇÃO: a tag correta é 'tecnicaUtilizada' — 'tecnica' não existe
                    # no schema TISS, por isso o valor nunca era encontrado/atualizado e uma
                    # tag inválida extra era criada.
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
                            # Insere logo após viaAcesso (se existir) ou após quantidadeExecutada
                            ref_apos = via_elem if via_elem is not None else quantidade_elem
                            proc_exec.insert(indice_apos(proc_exec, ref_apos), tec_elem)
                        detalhes_proc.append(f"Técnica ajustada: {tec_val}")
                        
                    if detalhes_proc: auditoria['procedimentos_ajustados'].append(f"Proc {cod_p}: " + " | ".join(detalhes_proc))

                # Ajustes de CBO e Código na Operadora para médicos cadastrados (Geral)
                for eq in equipes_restantes:
                    nome_prof_elem = eq.find('.//ans:nomeProf', NS)
                    nome_prof = nome_prof_elem.text.strip().upper() if nome_prof_elem is not None and nome_prof_elem.text else ""
                    
                    if nome_prof in set_conveniados: continue 
                    
                    cbo_elem = eq.find('.//ans:CBOS', NS)
                    if nome_prof in dict_medicos:
                        regra_m = dict_medicos[nome_prof]
                        cbo_novo = limpar_numero(regra_m['CBO Correto'])
                        if cbo_elem is not None and cbo_novo != '':
                            cbo_elem.text = cbo_novo
                            auditoria['cbos'].append(f"Médico(a) '{nome_prof}': CBO alterado para {cbo_novo}")
                        
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
                            h_novo, ok = calcular_tempo_oxigenio(h_ini.text, qtd_ex.text, cod_item)
                            if ok:
                                auditoria['oxigenio'].append(f"Oxigênio {cod_item}: Hora Final recalculada para {h_novo}")
                                h_fim.text = h_novo
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
        # 🛡️ Uma guia com problema (campo inesperado, formato divergente, etc.)
        # não derruba mais o processamento do lote inteiro — o erro é
        # registrado na auditoria e o processamento segue para as próximas guias.
        auditoria['erros'].append(f"Guia #{indice_guia} ({tipo_guia}): erro ao processar — {e}")

    # --- RECALCULO DE HASH MD5 ---
    hash_node = root.find('.//ans:hash', NS)
    if hash_node is not None: hash_node.text = ""

    temp_buffer = io.BytesIO()
    tree.write(temp_buffer, encoding='ISO-8859-1', xml_declaration=True)
    xml_bytes = temp_buffer.getvalue()
    xml_bytes = xml_bytes.replace(b"<?xml version='1.0' encoding='ISO-8859-1'?>", b'<?xml version="1.0" encoding="ISO-8859-1"?>')
    xml_bytes = xml_bytes.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')

    # 🛠️ CORREÇÃO: quando o texto do nó <ans:hash> é vazio, o serializador do
    # ElementTree escreve uma tag autofechada (<ans:hash/>) em vez de
    # <ans:hash></ans:hash>. Isso fazia o replace abaixo nunca encontrar a tag
    # e o hash final saía vazio. Normalizamos qualquer forma autofechada para
    # o formato aberto/fechado antes de calcular e inserir o MD5.
    xml_bytes = re.sub(rb'<ans:hash\s*/>', b'<ans:hash></ans:hash>', xml_bytes)

    md5_hash = hashlib.md5(xml_bytes).hexdigest()
    if hash_node is not None: xml_bytes = xml_bytes.replace(b'<ans:hash></ans:hash>', f'<ans:hash>{md5_hash}</ans:hash>'.encode('ISO-8859-1'))

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
    'erros': '⚠️ Avisos e Erros Durante o Processamento'
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
    📋 Copiar Código-Fonte para a Área de Transferência
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
                b.innerText = "📋 Copiar Código-Fonte para a Área de Transferência"; 
                b.style.backgroundColor = "#FFFFFF";
                b.style.color = "#1E1E1E";
                b.style.borderColor = "#CCCCCC";
            }}, 3000);
        }});
    }});
    </script>
    """
    components.html(html_copiar, height=50)

col1, col2 = st.columns([1, 1.2], gap="large")

with col1:
    with st.container(border=True):
        st.markdown("### 📜 Processamento do XML")
        st.markdown("Arraste o arquivo XML gerado pelo seu sistema aqui.")
        xml_up = st.file_uploader(
            "Arraste o arquivo XML", type=['xml'], label_visibility="collapsed",
            accept_multiple_files=False
        )

        if xml_up:
            st.caption(f"Arquivo selecionado: **{xml_up.name}**")
            if st.button("🚀 Iniciar Correção Automática", type="primary", use_container_width=True):
                dfs_atuais = {k: st.session_state[f'tab_{k}'] for k in tabelas_padrao.keys()}
                resultados_lote = []
                
                # Encapsula em uma lista para manter compatibilidade com a exibição de auditoria
                arquivo = xml_up
                resultado = {'nome': arquivo.name, 'xml_bytes': None, 'auditoria': None, 'falha_total': None}
                try:
                    xml_resultado, auditoria = processar_xml_tiss(arquivo, dfs_atuais)
                    resultado['xml_bytes'] = xml_resultado
                    resultado['auditoria'] = auditoria
                except Exception as e:
                    resultado['falha_total'] = str(e)
                
                resultados_lote.append(resultado)
                st.session_state['resultados_lote'] = resultados_lote

with col2:
    if 'resultados_lote' in st.session_state and st.session_state['resultados_lote']:
        resultado = st.session_state['resultados_lote'][0]

        with st.container(border=True):
            st.markdown("### 📊 Resultado da Auditoria")

            if resultado.get('falha_total'):
                st.error(f"❌ **{resultado['nome']}**: {resultado['falha_total']}")
            else:
                st.success(f"✅ Arquivo **{resultado['nome']}** processado com sucesso!")

                # Converte bytes para texto tratando a codificação ISO-8859-1 comum em XMLs TISS
                xml_bytes = resultado['xml_bytes']
                try:
                    xml_texto = xml_bytes.decode('ISO-8859-1')
                except (UnicodeDecodeError, AttributeError):
                    xml_texto = xml_bytes.decode('utf-8', errors='replace') if isinstance(xml_bytes, bytes) else xml_bytes

                st.divider()

                # Botões de Ação Principais em destaque
                st.markdown("#### 🚀 Ações")
                c1, c2 = st.columns(2)
                
                with c1:
                    st.download_button(
                        label="📥 Baixar XML Validado",
                        data=xml_bytes,
                        file_name=f"PRONTO_{resultado['nome']}",
                        mime="application/xml",
                        type="primary",
                        use_container_width=True
                    )
                    
                with c2:
                    # Usa a sua função original para o botão de cópia ficar grande e lado a lado
                    botao_copiar_codigo(xml_texto, key_sufixo="copia_xml_unico")

                st.divider()

                # Esconde o relatório até ser clicado
                with st.expander("📝 Ver Detalhes das Modificações"):
                    aud = resultado.get('auditoria', {})
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

                # Esconde o código XML completo até ser clicado
                with st.expander("🔍 Inspecionar Código Visualmente"):
                    st.code(xml_texto, language='xml')

    else:
        with st.container(border=True):
            st.info("Aguardando arquivo XML. Faça o upload na coluna ao lado.")

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
