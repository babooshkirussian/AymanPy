"""
Snake Game
==========
A classic Snake game built with pygame.

Controls:
  Arrow keys / WASD - move
  P                 - pause / resume
  R                 - restart after game over
  Esc               - quit

Run with:  python snake_game.py
"""

import random
import sys
import pygame

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CELL_SIZE = 24
GRID_WIDTH = 25
GRID_HEIGHT = 20
SCREEN_WIDTH = CELL_SIZE * GRID_WIDTH
SCREEN_HEIGHT = CELL_SIZE * GRID_HEIGHT + 60  # extra space for score bar
FPS_START = 8
FPS_MAX = 18

COLOR_BG = (18, 18, 24)
COLOR_GRID = (28, 28, 36)
COLOR_SNAKE_HEAD = (98, 214, 138)
COLOR_SNAKE_BODY = (58, 168, 108)
COLOR_FOOD = (232, 93, 93)
COLOR_TEXT = (235, 235, 235)
COLOR_ACCENT = (240, 200, 90)

UP = (0, -1)
DOWN = (0, 1)
LEFT = (-1, 0)
RIGHT = (1, 0)


class SnakeGame:
    def __init__(self):
        pygame.init()
        pygame.display.set_caption("Snake")
        self.screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
        self.clock = pygame.time.Clock()
        self.font_big = pygame.font.SysFont("consolas", 40, bold=True)
        self.font_med = pygame.font.SysFont("consolas", 24, bold=True)
        self.font_small = pygame.font.SysFont("consolas", 18)
        self.reset()

    def reset(self):
        cx, cy = GRID_WIDTH // 2, GRID_HEIGHT // 2
        self.snake = [(cx - 1, cy), (cx - 2, cy), (cx - 3, cy)]
        self.direction = RIGHT
        self.next_direction = RIGHT
        self.food = self.spawn_food()
        self.score = 0
        self.speed = FPS_START
        self.game_over = False
        self.paused = False

    def spawn_food(self):
        while True:
            pos = (random.randint(0, GRID_WIDTH - 1), random.randint(0, GRID_HEIGHT - 1))
            if pos not in self.snake:
                return pos

    def handle_input(self):
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.quit()
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    self.quit()
                elif event.key in (pygame.K_UP, pygame.K_w) and self.direction != DOWN:
                    self.next_direction = UP
                elif event.key in (pygame.K_DOWN, pygame.K_s) and self.direction != UP:
                    self.next_direction = DOWN
                elif event.key in (pygame.K_LEFT, pygame.K_a) and self.direction != RIGHT:
                    self.next_direction = LEFT
                elif event.key in (pygame.K_RIGHT, pygame.K_d) and self.direction != LEFT:
                    self.next_direction = RIGHT
                elif event.key == pygame.K_p and not self.game_over:
                    self.paused = not self.paused
                elif event.key == pygame.K_r and self.game_over:
                    self.reset()

    def update(self):
        if self.game_over or self.paused:
            return

        self.direction = self.next_direction
        head_x, head_y = self.snake[0]
        dx, dy = self.direction
        new_head = (head_x + dx, head_y + dy)

        # Wall collision
        if not (0 <= new_head[0] < GRID_WIDTH and 0 <= new_head[1] < GRID_HEIGHT):
            self.game_over = True
            return

        # Self collision
        if new_head in self.snake:
            self.game_over = True
            return

        self.snake.insert(0, new_head)

        if new_head == self.food:
            self.score += 1
            self.speed = min(FPS_MAX, FPS_START + self.score // 3)
            self.food = self.spawn_food()
        else:
            self.snake.pop()

    def draw_grid(self):
        for x in range(GRID_WIDTH):
            for y in range(GRID_HEIGHT):
                if (x + y) % 2 == 0:
                    rect = pygame.Rect(x * CELL_SIZE, y * CELL_SIZE + 60, CELL_SIZE, CELL_SIZE)
                    pygame.draw.rect(self.screen, COLOR_GRID, rect)

    def draw_snake(self):
        for i, (x, y) in enumerate(self.snake):
            rect = pygame.Rect(x * CELL_SIZE + 1, y * CELL_SIZE + 61, CELL_SIZE - 2, CELL_SIZE - 2)
            color = COLOR_SNAKE_HEAD if i == 0 else COLOR_SNAKE_BODY
            pygame.draw.rect(self.screen, color, rect, border_radius=6)

    def draw_food(self):
        x, y = self.food
        rect = pygame.Rect(x * CELL_SIZE + 3, y * CELL_SIZE + 63, CELL_SIZE - 6, CELL_SIZE - 6)
        pygame.draw.rect(self.screen, COLOR_FOOD, rect, border_radius=8)

    def draw_ui(self):
        pygame.draw.rect(self.screen, (12, 12, 16), (0, 0, SCREEN_WIDTH, 60))
        score_surf = self.font_med.render(f"Score: {self.score}", True, COLOR_TEXT)
        self.screen.blit(score_surf, (16, 16))

        hint = "P: pause" if not self.game_over else "R: restart"
        hint_surf = self.font_small.render(hint, True, (150, 150, 150))
        self.screen.blit(hint_surf, (SCREEN_WIDTH - hint_surf.get_width() - 16, 20))

        if self.paused and not self.game_over:
            self.center_text("PAUSED", self.font_big, COLOR_ACCENT)

        if self.game_over:
            self.center_text("GAME OVER", self.font_big, COLOR_FOOD, y_offset=-20)
            sub = self.font_med.render("Press R to restart", True, COLOR_TEXT)
            self.screen.blit(sub, (SCREEN_WIDTH // 2 - sub.get_width() // 2, SCREEN_HEIGHT // 2 + 25))

    def center_text(self, text, font, color, y_offset=0):
        surf = font.render(text, True, color)
        rect = surf.get_rect(center=(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2 + y_offset))
        self.screen.blit(surf, rect)

    def draw(self):
        self.screen.fill(COLOR_BG)
        self.draw_grid()
        self.draw_food()
        self.draw_snake()
        self.draw_ui()
        pygame.display.flip()

    def quit(self):
        pygame.quit()
        sys.exit()

    def run(self):
        while True:
            self.handle_input()
            self.update()
            self.draw()
            self.clock.tick(self.speed)


if __name__ == "__main__":
    SnakeGame().run()
