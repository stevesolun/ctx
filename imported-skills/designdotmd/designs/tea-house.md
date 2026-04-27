---
version: alpha
name: Tea House
description: Matcha ceremony: rice paper, bamboo green, clay red.
colors:
  primary: "#1E2016"
  secondary: "#7B8069"
  tertiary: "#B0361B"
  neutral: "#F0EBDA"
  surface: "#F8F3E1"
  on-primary: "#F8F3E1"
typography:
  display:
    fontFamily: Shippori Mincho
    fontSize: 4.5rem
    fontWeight: 400
    letterSpacing: "-0.015em"
  h1:
    fontFamily: Shippori Mincho
    fontSize: 2.4rem
    fontWeight: 400
  body:
    fontFamily: Noto Serif JP
    fontSize: 1rem
    lineHeight: 1.75
  label:
    fontFamily: Noto Sans JP
    fontSize: 0.72rem
    letterSpacing: "0.2em"
rounded:
  sm: 2px
  md: 4px
  lg: 8px
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

A tea-house palette: rice paper surface, bamboo-green primary, clay-red seal accents.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#1E2016`):** Headlines and core text.
- **Secondary (`#7B8069`):** Borders, captions, and metadata.
- **Tertiary (`#B0361B`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F0EBDA`):** The page foundation.

## Typography

- **display:** Shippori Mincho 4.5rem
- **h1:** Shippori Mincho 2.4rem
- **body:** Noto Serif JP 1rem
- **label:** Noto Sans JP 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
