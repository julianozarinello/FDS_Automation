# Validação técnica — 20/07/2026

Este registro explica a base das otimizações e os testes rápidos executados.
Desempenho de FDS depende do caso e do computador; por isso as medições abaixo
são indicativas e o aplicativo testa cada caso antes de guardar a escolha em
cache.

## Base técnica oficial

- [Página de manuais do NIST/FDS](https://pages.nist.gov/fds/manuals.html).
- [FDS User's Guide 6.11.1](https://github.com/firemodels/fds/releases/download/FDS-6.11.1/FDS_User_Guide.pdf),
  release atual publicada quando esta revisão foi feita.
- [Fonte atual do User's Guide](https://github.com/firemodels/fds/blob/master/Manuals/FDS_User_Guide/FDS_User_Guide.tex).
- [Fonte do leitor de entrada do FDS](https://github.com/firemodels/fds/blob/master/Source/read.f90),
  usada para conferir regras de `MESH`, `MPI_PROCESS`, `MULT_ID` e `CATF`.
- [OpenMP Notes](https://github.com/firemodels/fds/wiki/OpenMP-Notes).
- [Timing Benchmarks](https://github.com/firemodels/fds/tree/master/Verification/Timing_Benchmarks).

As recomendações aplicadas são: usar cargas comparáveis por processo MPI;
manter `MPI_PROCESS` contínuo e monotonicamente crescente; preferir núcleos
físicos a SMT/Hyper-Threading; não presumir que mais threads OpenMP serão mais
rápidas; e inspecionar `VELOCITY_ERROR` quando novas interfaces são criadas.

## Decisões implementadas

- O otimizador compara SERIAL, OpenMP com 1/2/4/total de threads, MPI e híbrido.
  O modo híbrido usa `fds_openmp.exe`; MPI puro usa `fds.exe`.
- A medição usa o avanço entre amostras de tempo simulado, reduzindo o peso da
  inicialização e de `T_BEGIN`/restart. Cache de schema 3 inclui caso, binários,
  hardware, modo, afinidade, MKL e opções relevantes.
- `MKL_NUM_THREADS` fica em 1 somente para MPI puro; OpenMP, híbrido e SERIAL
  podem testar a quantidade de threads adequada ao solver UGLMAT.
- A atribuição MPI usa a partição ótima em blocos contíguos sem mudar a ordem
  dos namelists. Configurações com mais ranks que meshes são reduzidas antes da
  execução, e todos os valores de `MPI_PROCESS` são validados pelas regras do
  leitor atual do FDS.
- O parser de namelists respeita comentários, aspas Fortran, barras e vírgulas
  dentro de strings. Coordenadas usam precisão de round-trip IEEE-754 (`.17g`).
- `MULT_ID` e `CATF` tornam a quantidade efetiva de meshes desconhecida antes
  do pré-processamento do FDS. Nesses casos, o programa preserva o arquivo e não
  faz clamp, splitter ou otimização automática baseada na contagem literal.
- A divisão geométrica é opt-in. Ela recua automaticamente em meshes
  cilíndricas, transformações, `MESH_NUMBER`, `CSVF` e restart. Cada metade
  precisa manter ao menos quatro células no eixo cortado.

## Testes automatizados

Comando:

```powershell
python -m unittest discover -s tests -v
```

Resultado final da v1.18: **47 testes aprovados em menos de 0,7 s**. A
suíte cobre conservação de células/geometria, partição ótima, parser e
regravação, precisão, guardas de divisão, validação MPI, comandos SERIAL/MPI/
híbrido, score, cache, MKL, CPUs híbridas e processor groups, atualização de
binários, falhas de fila, cancelamento, GUI, YAML e planilha atômica. A v1.18
acrescenta diagnósticos estruturados, localização do `.out` pelo `CHID`,
proteção contra pedidos de parada obsoletos e um teste real com subprocesso
lento que confirma a interrupção individual sem encerrar a fila.

Um teste de propriedade adicional gerou 4.500 combinações aleatórias de cargas.
Cada partição contígua foi comparada com a solução exata por força bruta:
**4.500/4.500 coincidiram**, em aproximadamente 0,6 s.

## Ensaios curtos com FDS real

Caso oficial: `Examples/Timing_Benchmarks/openmp_test64a.fds`. Ambiente:
Windows e FDS 6.11.0 instalado, com janelas de aproximadamente 20 s.

| OpenMP | Avanço medido | Índice relativo a 1 thread |
|---:|---:|---:|
| 1 thread | 0,02887 s simulados/s real | 1,00 |
| 2 threads | 0,03776 s simulados/s real | 1,31 |
| 4 threads | 0,05325 s simulados/s real | 1,84 |

Também foram executados casos mínimos para validar o corte. Filhas com
`IJK=4,1,2` e `4,4,2` pararam com `ERROR(426): Poisson initialization`; por
isso o splitter agora exige pelo menos quatro células em cada metade. A suíte
leu ainda quatro casos FDS reais com 24, 24, 18 e 25 meshes, obtendo a mesma
contagem no scanner novo e no fluxo anterior.

## Limites da medição

Uma janela fixa de relógio pode fazer candidatos rápidos e lentos alcançarem
fases físicas diferentes do incêndio, cujo custo por passo não é idêntico. O
cache e o limiar de ganho reduzem escolhas instáveis, mas não substituem um
estudo de strong scaling com número igual de passos e repetições. Para uma
decisão de projeto, compare os dois melhores modos por mais tempo.

## Verificação antes de uso em projeto

Confirme que o FDS termina normalmente e revise o relatório da fila. Se a
divisão avançada for habilitada, compare original × otimizado e avalie as
grandezas de interesse, além de `VELOCITY_ERROR` nas interfaces. Otimização
computacional não substitui validação nem julgamento de engenharia.
