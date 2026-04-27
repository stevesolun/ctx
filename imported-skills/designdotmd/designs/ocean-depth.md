---
version: alpha
name: Ocean Depth
description: Midnight blue foundation with a teal pulse.
colors:
  primary: "#E8F0F5"
  secondary: "#8FA5B5"
  tertiary: "#3CBAB2"
  neutral: "#0B1A28"
  surface: "#142637"
  on-primary: "#0B1A28"
typography:
  display:
    fontFamily: Instrument Serif
    fontSize: 4.5rem
    fontWeight: 400
    letterSpacing: "-0.015em"
  h1:
    fontFamily: Instrument Serif
    fontSize: 2.5rem
    fontWeight: 400
  body:
    fontFamily: Inter
    fontSize: 1rem
    lineHeight: 1.6
  label:
    fontFamily: JetBrains Mono
    fontSize: 0.75rem
    letterSpacing: "0.04em"
rounded:
  sm: 6px
  md: 12px
  lg: 20px
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

A deep, calm product palette. Midnight-blue surfaces, muted steel for support, and an oxidised teal for interaction.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#E8F0F5`):** Headlines and core text.
- **Secondary (`#8FA5B5`):** Borders, captions, and metadata.
- **Tertiary (`#3CBAB2`):** The sole driver for interaction. Reserve it.
- **Neutral (`#0B1A28`):** The page foundation.

## Typography

- **display:** Instrument Serif 4.5rem
- **h1:** Instrument Serif 2.5rem
- **body:** Inter 1rem
- **label:** JetBrains Mono 0.75rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
