---
version: alpha
name: AI Labs
description: Research-paper white, terminal-green prompts.
colors:
  primary: "#0F1112"
  secondary: "#6F7478"
  tertiary: "#00A36C"
  neutral: "#FAFAF8"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: IBM Plex Sans
    fontSize: 3.5rem
    fontWeight: 500
    letterSpacing: "-0.02em"
  h1:
    fontFamily: IBM Plex Sans
    fontSize: 2rem
    fontWeight: 500
  body:
    fontFamily: IBM Plex Sans
    fontSize: 0.95rem
    lineHeight: 1.6
  label:
    fontFamily: IBM Plex Mono
    fontSize: 0.72rem
rounded:
  sm: 3px
  md: 5px
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

Clean research-console aesthetic: bright paper, thin hairlines, matrix-green for model responses.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#0F1112`):** Headlines and core text.
- **Secondary (`#6F7478`):** Borders, captions, and metadata.
- **Tertiary (`#00A36C`):** The sole driver for interaction. Reserve it.
- **Neutral (`#FAFAF8`):** The page foundation.

## Typography

- **display:** IBM Plex Sans 3.5rem
- **h1:** IBM Plex Sans 2rem
- **body:** IBM Plex Sans 0.95rem
- **label:** IBM Plex Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
