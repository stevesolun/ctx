---
version: alpha
name: OSS Terminal
description: README-first OSS: bone paper, diff green, PR purple.
colors:
  primary: "#161614"
  secondary: "#6A6860"
  tertiary: "#8957E5"
  neutral: "#F6F1E7"
  surface: "#FBF6EA"
  on-primary: "#FBF6EA"
typography:
  display:
    fontFamily: JetBrains Mono
    fontSize: 3.25rem
    fontWeight: 700
    letterSpacing: "-0.02em"
  h1:
    fontFamily: JetBrains Mono
    fontSize: 1.7rem
    fontWeight: 600
  body:
    fontFamily: IBM Plex Sans
    fontSize: 0.95rem
    lineHeight: 1.65
  label:
    fontFamily: JetBrains Mono
    fontSize: 0.72rem
    letterSpacing: "0.04em"
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

An open-source docs palette: bone-paper surface, diff-green additions, PR-purple accent.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#161614`):** Headlines and core text.
- **Secondary (`#6A6860`):** Borders, captions, and metadata.
- **Tertiary (`#8957E5`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F6F1E7`):** The page foundation.

## Typography

- **display:** JetBrains Mono 3.25rem
- **h1:** JetBrains Mono 1.7rem
- **body:** IBM Plex Sans 0.95rem
- **label:** JetBrains Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
