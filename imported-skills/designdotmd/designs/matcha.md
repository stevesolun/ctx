---
version: alpha
name: Matcha
description: Soft green, bamboo, steam.
colors:
  primary: "#2B3A2E"
  secondary: "#7A8A72"
  tertiary: "#6F9A5B"
  neutral: "#ECE8DB"
  surface: "#F7F3E7"
  on-primary: "#F7F3E7"
typography:
  display:
    fontFamily: Noto Serif
    fontSize: 4rem
    fontWeight: 500
    letterSpacing: "-0.015em"
  h1:
    fontFamily: Noto Serif
    fontSize: 2.5rem
    fontWeight: 500
  body:
    fontFamily: Noto Sans
    fontSize: 1rem
    lineHeight: 1.65
  label:
    fontFamily: Noto Sans
    fontSize: 0.75rem
    letterSpacing: "0.08em"
rounded:
  sm: 6px
  md: 12px
  lg: 24px
spacing:
  sm: 8px
  md: 16px
  lg: 32px
components:
  button-primary:
    backgroundColor: "{colors.tertiary}"
    textColor: "{colors.on-primary}"
    rounded: "{rounded.md}"
    padding: 12px 20px
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    rounded: "{rounded.lg}"
    padding: 24px
---
## Overview

A calming wellness palette without the spa cliches. Muted matcha green, warm oat, quiet ink.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#2B3A2E`):** Headlines and core text.
- **Secondary (`#7A8A72`):** Borders, captions, and metadata.
- **Tertiary (`#6F9A5B`):** The sole driver for interaction. Reserve it.
- **Neutral (`#ECE8DB`):** The page foundation.

## Typography

- **display:** Noto Serif 4rem
- **h1:** Noto Serif 2.5rem
- **body:** Noto Sans 1rem
- **label:** Noto Sans 0.75rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
