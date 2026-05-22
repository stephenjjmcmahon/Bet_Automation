import sys
import os

# allow imports from project
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.ai_interpreter import AIInterpreter


def test_ai_interpreter_print():
    user_input = "Bet Chelsea 100 at 5.2"

    print("User input:")
    print(user_input)

    parsed_bet = AIInterpreter.interpret(user_input)

    print("\nAI interpreter output:")
    print(parsed_bet)


if __name__ == "__main__":
    test_ai_interpreter_print()
