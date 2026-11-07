# Agente Monitor de Pregações / Sermon Monitor Agent

## 🇧🇷 Português

Script em Python que monitora canais do YouTube, extrai a transcrição das pregações e usa a API do Google Gemini para gerar uma estratégia de conteúdo (ideias de posts, roteiros de Reels, trechos para cortes e calendário de publicação).

**Como usar**

1. Instale as dependências: `pip install -r requirements.txt`
2. Crie um arquivo `.env` na pasta do projeto com a sua chave do Gemini: `GEMINI_API_KEY=sua_chave_aqui`
3. Rode: `python monitor_pregacoes.py --video "LINK_DO_VIDEO"`

O resultado é salvo na pasta `estrategias/`.

## 🇺🇸 English

A Python script that monitors YouTube channels, extracts sermon transcripts and uses the Google Gemini API to generate a content strategy (post ideas, Reels scripts, clip suggestions and a publishing calendar).

**How to use**

1. Install the dependencies: `pip install -r requirements.txt`
2. Create a `.env` file in the project folder with your Gemini key: `GEMINI_API_KEY=your_key_here`
3. Run: `python monitor_pregacoes.py --video "VIDEO_LINK"`

The result is saved in the `estrategias/` folder.
