#pragma once
#include <istream>
#include <stdexcept>
#include <string>

// Shared native pipe bound; never use unbounded getline on parent input.
template<class Error = std::runtime_error>
bool read_private_command(std::istream & input, std::string & line) {
    line.clear();
    char character;
    while (input.get(character)) {
        if (character == '\n') return true;
        if (line.size() == 16384) throw Error("command_limit");
        line.push_back(character);
    }
    if (!line.empty()) throw Error("incomplete_command");
    return false;
}
