---
version: alpha
name: Notion Beige
description: Workspace-calm: warm beige, soft ink, lots of breathing.
colors:
  primary: "#191918"
  secondary: "#8C877D"
  tertiary: "#C26B5B"
  neutral: "#F7F6F3"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Inter
    fontSize: 3.5rem
    fontWeight: 700
    letterSpacing: "-0.03em"
  h1:
    fontFamily: Inter
    fontSize: 2rem
    fontWeight: 700
    letterSpacing: "-0.02em"
  body:
    fontFamily: Inter
    fontSize: 0.95rem
    lineHeight: 1.6
  label:
    fontFamily: Inter
    fontSize: 0.72rem
    letterSpacing: "0.02em"
rounded:
  sm: 4px
  md: 6px
  lg: 10px
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

A gentle, document-first palette. Warm off-white body, near-black text, restrained coral for mentions and active states.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#191918`):** Headlines and core text.
- **Secondary (`#8C877D`):** Borders, captions, and metadata.
- **Tertiary (`#C26B5B`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F7F6F3`):** The page foundation.

## Typography

- **display:** Inter 3.5rem
- **h1:** Inter 2rem
- **body:** Inter 0.95rem
- **label:** Inter 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
