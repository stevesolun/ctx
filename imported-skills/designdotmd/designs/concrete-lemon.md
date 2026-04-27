---
version: alpha
name: Concrete Lemon
description: Raw concrete, structural grid, lemon accent.
colors:
  primary: "#1A1A1A"
  secondary: "#6B6B6B"
  tertiary: "#D4E157"
  neutral: "#D9D6D0"
  surface: "#F0EDE6"
  on-primary: "#1A1A1A"
typography:
  display:
    fontFamily: Archivo
    fontSize: 4.5rem
    fontWeight: 800
    letterSpacing: "-0.04em"
  h1:
    fontFamily: Archivo
    fontSize: 2.5rem
    fontWeight: 800
  body:
    fontFamily: Archivo
    fontSize: 0.95rem
    lineHeight: 1.5
  label:
    fontFamily: Archivo Narrow
    fontSize: 0.72rem
    letterSpacing: "0.1em"
rounded:
  sm: 0px
  md: 0px
  lg: 2px
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

Brutalist restraint with a jolt. Concrete greys, hard sans, a single high-visibility lemon for wayfinding.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#1A1A1A`):** Headlines and core text.
- **Secondary (`#6B6B6B`):** Borders, captions, and metadata.
- **Tertiary (`#D4E157`):** The sole driver for interaction. Reserve it.
- **Neutral (`#D9D6D0`):** The page foundation.

## Typography

- **display:** Archivo 4.5rem
- **h1:** Archivo 2.5rem
- **body:** Archivo 0.95rem
- **label:** Archivo Narrow 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
