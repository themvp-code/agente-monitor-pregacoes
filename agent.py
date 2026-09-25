from google.adk.agents import LlmAgent
from google.adk.tools import agent_tool
from google.adk.tools.google_search_tool import GoogleSearchTool
from google.adk.tools import url_context

assistente_de_conte_do_sm_prega_oes_google_search_agent = LlmAgent(
  name='Assistente_de_conte_do_SM_prega_oes_google_search_agent',
  model='gemini-2.5-flash',
  description=(
      'Agent specialized in performing Google searches.'
  ),
  sub_agents=[],
  instruction='Use the GoogleSearchTool to find information on the web.',
  tools=[
    GoogleSearchTool()
  ],
)
assistente_de_conte_do_sm_prega_oes_url_context_agent = LlmAgent(
  name='Assistente_de_conte_do_SM_prega_oes_url_context_agent',
  model='gemini-2.5-flash',
  description=(
      'Agent specialized in fetching content from URLs.'
  ),
  sub_agents=[],
  instruction='Use the UrlContextTool to retrieve content from provided URLs.',
  tools=[
    url_context
  ],
)
root_agent = LlmAgent(
  name='Assistente_de_conte_do_SM_prega_oes',
  model='gemini-2.5-flash',
  description=(
      'agente que monitora o YouTube em busca de vídeos novos de pregação do Pastor Genival Bento e do Pastor Edivaldo Filho, verificando se eles aparecem no título, descrição ou legenda de cada vídeo encontrado.'
  ),
  sub_agents=[],
  instruction='Você é um assistente que monitora o YouTube em busca de vídeos em português que mencionem ou apresentem o Pastor Genival Bento ou o Pastor Edivaldo Filho, publicados nos últimos trinta dias. Ignore vídeos publicados nos canais oficiais desses pastores, focando apenas em vídeos publicados por outros canais, como cortes, compilações, reações ou reproduções feitas por terceiros. Para cada vídeo encontrado, verifique se o nome do pastor aparece no título, na descrição ou na legenda automática do vídeo. Sempre inclua na resposta o link direto do vídeo, o nome do canal que publicou, a data de publicação, e em qual desses locais o nome foi identificado. Ignore vídeos em outros idiomas que não sejam português. Se não encontrar nenhum vídeo dentro desses trinta dias, informe claramente que nenhum vídeo recente foi encontrado.',
  tools=[
    agent_tool.AgentTool(agent=assistente_de_conte_do_sm_prega_oes_google_search_agent),
    agent_tool.AgentTool(agent=assistente_de_conte_do_sm_prega_oes_url_context_agent)
  ],
)
