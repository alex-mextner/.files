default: trim

trim:
	g++ src/trim.cpp -Wall -O3 -o bin/trim

zsh-completion-check:
	zsh -df tests/zsh-completion-check.zsh
