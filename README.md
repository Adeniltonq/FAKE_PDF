# Gerador de PDFs Aleatórios

Programa em **Python puro (só a biblioteca padrão)** para criar PDFs de teste:

1. **Embaralhar páginas** — pega PDFs que já existem, sorteia e redistribui as páginas em novos arquivos.
2. **Meus textos + minhas fotos** — gera PDFs com o texto e as fotos que você informar (sorteando layout, tamanho e páginas).
   - **Sorteio do conteúdo**: corta as frases em pedaços, remonta, repete e intercala as fotos
     (inteiras e recortadas) até preencher **exatamente** o número de páginas pedido
     (10, 100, 200...), com quantos PDFs você quiser.
3. **Aleatório automático** — cria PDFs do zero com textos aleatórios em português, no tamanho que você quiser.

Não precisa de `pip install`. Funciona em **Linux, Windows e macOS**, com interface **Tkinter** no computador e **modo terminal** (teclado) no celular/Termux, SSH e servidores.

## Como rodar

```bash
python gerador_pdf.py              # abre a janela (ou o terminal, se não houver tela)
python gerador_pdf.py --terminal   # força o modo terminal (menus numéricos)
python gerador_pdf.py --grafico    # força a janela gráfica
python gerador_pdf.py --cli ajuda  # linha de comando completa
```

Requisitos: Python 3.8+ (testado no 3.14). O Tkinter vem junto com o Python oficial do Windows/macOS. No Linux:
`sudo apt install python3-tk` (Debian/Ubuntu) ou `sudo dnf install python3-tkinter` (Fedora).

### Celular (Termux/Android)

O programa detecta o Termux e **usa apenas o terminal** automaticamente:

```bash
pkg install python
python gerador_pdf.py --terminal
```

Não é preciso instalar `python-tk` no celular. No editor de arquivos do Android, dá para copiar caminhos
de `/storage/emulated/0/Download/...` e colar no programa.

## Linha de comando (rápido)

```bash
# 1) sortear páginas de PDFs existentes
python gerador_pdf.py --cli misturar a.pdf b.pdf -p 12 -n 3

# 2) usar meus textos e minhas fotos
python gerador_pdf.py --cli texto --texto cartas.txt --foto foto1.jpg --foto foto2.png -p 4 --pdfs 5

# 2b) sorteio do conteúdo: 20 PDFs de 100 páginas com frases cortadas/repetidas + fotos
python gerador_pdf.py --cli texto --texto cartas.txt --foto ./fotos/ --sorteio -p 100 --pdfs 20

# 3) PDFs aleatórios automáticos, com tamanho sorteado
python gerador_pdf.py --cli aleatorio --pdfs 10 -p 3 --aleatorio-tamanho --largura-min 80 --largura-max 200
```

Opções do sorteio: `--sorteio`, `--recortes N` (versões recortadas de cada foto),
`--so-meu-texto` (não mistura frases aleatórias), `--semente N` (repete o mesmo resultado).

Opções úteis: `-o PASTA` (saída), `--tamanho A4|A5|A6|A3|B5|Letter|Legal|10x15cm|210x297mm|400x600pt`,
`--aleatorio-tamanho`, `--aleatorio-paginas MIN MAX`, `--marca-dagua TEXTO`, `--fonte-serif`,
`--sem-repetir`, `--nao-aleatorizar`.

Os arquivos são gravados por padrão na pasta `saida/` ao lado do programa.

## O que ele sabe fazer

- Ler PDFs com xref clássico, xref streams e object streams, reconstruindo arquivos danificados;
- Copiar páginas entre PDFs preservando textos, vetores e imagens;
- Embarcar **JPG** (sem recomprimir) e **PNG** (paleta, tons de cinza, RGBA, 16 bits) decodificados internamente;
- Desenhar texto com as fontes padrão do PDF (Helvetica, Times, Courier) e acentuação em WinAnsi;
- Tamanhos A3/A4/A5/Carta/Legal e tamanhos personalizados (mm, cm, pt ou polegadas);
- Tabelas, listas, títulos, legendas de foto e marca d'água diagonal.

## Limitações

- PDFs protegidos por senha/criptografados não são aceitos (a mensagem avisa);
- PNG entrelaçado não é suportado (salve novamente como PNG comum);
- É um gerador local: use-o para testes, treinamentos e simulações — não para falsificar documentos
  ou obter vantagens indevidas.

## Estrutura

```
gerador_pdf.py   # programa completo (leitor/escritor de PDF, geradores, terminal e Tkinter)
```
