---
version: alpha
name: Streetwear Block
description: Supreme-energy: box logo, off-white, siren red.
colors:
  primary: "#0A0A0A"
  secondary: "#6B6B6B"
  tertiary: "#E02020"
  neutral: "#EEEBE2"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Anton
    fontSize: 5.5rem
    fontWeight: 400
    letterSpacing: "-0.01em"
  h1:
    fontFamily: Anton
    fontSize: 3rem
    fontWeight: 400
  body:
    fontFamily: Inter
    fontSize: 0.92rem
    lineHeight: 1.55
  label:
    fontFamily: Anton
    fontSize: 0.82rem
    letterSpacing: "0.12em"
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

A dropwear system that lives and dies on the drop.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#0A0A0A`):** Headlines and core text.
- **Secondary (`#6B6B6B`):** Borders, captions, and metadata.
- **Tertiary (`#E02020`):** The sole driver for interaction. Reserve it.
- **Neutral (`#EEEBE2`):** The page foundation.

## Typography

- **display:** Anton 5.5rem
- **h1:** Anton 3rem
- **body:** Inter 0.92rem
- **label:** Anton 0.82rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
