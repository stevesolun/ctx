---
version: alpha
name: Atelier Noir
description: Fashion-week black, silk ivory, thin everything.
colors:
  primary: "#111110"
  secondary: "#7A7670"
  tertiary: "#8C6A3F"
  neutral: "#F2EEE5"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Italiana
    fontSize: 5.5rem
    fontWeight: 400
    letterSpacing: "0.01em"
  h1:
    fontFamily: Italiana
    fontSize: 2.75rem
    fontWeight: 400
  body:
    fontFamily: Jost
    fontSize: 0.95rem
    lineHeight: 1.65
  label:
    fontFamily: Jost
    fontSize: 0.72rem
    letterSpacing: "0.22em"
rounded:
  sm: 0px
  md: 0px
  lg: 0px
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

A runway-minimal system: ivory paper, charcoal text, ultra-tight type.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#111110`):** Headlines and core text.
- **Secondary (`#7A7670`):** Borders, captions, and metadata.
- **Tertiary (`#8C6A3F`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F2EEE5`):** The page foundation.

## Typography

- **display:** Italiana 5.5rem
- **h1:** Italiana 2.75rem
- **body:** Jost 0.95rem
- **label:** Jost 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
