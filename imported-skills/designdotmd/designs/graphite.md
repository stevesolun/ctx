---
version: alpha
name: Graphite
description: Cool greys, one lime signal.
colors:
  primary: "#ECEDEE"
  secondary: "#9CA3AF"
  tertiary: "#B4FF39"
  neutral: "#0E1013"
  surface: "#17191C"
  on-primary: "#0E1013"
typography:
  display:
    fontFamily: Inter Tight
    fontSize: 4rem
    fontWeight: 600
    letterSpacing: "-0.03em"
  h1:
    fontFamily: Inter Tight
    fontSize: 2.25rem
    fontWeight: 600
  body:
    fontFamily: Inter
    fontSize: 0.95rem
    lineHeight: 1.55
  label:
    fontFamily: JetBrains Mono
    fontSize: 0.75rem
    letterSpacing: "0.02em"
rounded:
  sm: 6px
  md: 10px
  lg: 14px
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

An engineering-grade dark palette. Carefully tuned greys across 10 stops, with a single lime-green for focus and CTAs.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#ECEDEE`):** Headlines and core text.
- **Secondary (`#9CA3AF`):** Borders, captions, and metadata.
- **Tertiary (`#B4FF39`):** The sole driver for interaction. Reserve it.
- **Neutral (`#0E1013`):** The page foundation.

## Typography

- **display:** Inter Tight 4rem
- **h1:** Inter Tight 2.25rem
- **body:** Inter 0.95rem
- **label:** JetBrains Mono 0.75rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
