---
version: alpha
name: Field Notes
description: Botanist's notebook: moss, bark, pressed leaves.
colors:
  primary: "#1F2A1A"
  secondary: "#7B8473"
  tertiary: "#6B8E3D"
  neutral: "#EEE9DB"
  surface: "#F8F3E3"
  on-primary: "#F8F3E3"
typography:
  display:
    fontFamily: Cormorant Garamond
    fontSize: 4.5rem
    fontWeight: 500
  h1:
    fontFamily: Cormorant Garamond
    fontSize: 2.4rem
    fontWeight: 500
  body:
    fontFamily: Libre Caslon Text
    fontSize: 1.02rem
    lineHeight: 1.7
  label:
    fontFamily: Space Mono
    fontSize: 0.72rem
    letterSpacing: "0.1em"
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

An outdoor-brand system grounded in field sketches.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#1F2A1A`):** Headlines and core text.
- **Secondary (`#7B8473`):** Borders, captions, and metadata.
- **Tertiary (`#6B8E3D`):** The sole driver for interaction. Reserve it.
- **Neutral (`#EEE9DB`):** The page foundation.

## Typography

- **display:** Cormorant Garamond 4.5rem
- **h1:** Cormorant Garamond 2.4rem
- **body:** Libre Caslon Text 1.02rem
- **label:** Space Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
