---
version: alpha
name: Cyberpunk City
description: Neon signage, acid yellow, deep violet.
colors:
  primary: "#F8F7FF"
  secondary: "#8E8AC4"
  tertiary: "#F0FF00"
  neutral: "#110A24"
  surface: "#1B1236"
  on-primary: "#110A24"
typography:
  display:
    fontFamily: Orbitron
    fontSize: 4rem
    fontWeight: 700
    letterSpacing: "-0.02em"
  h1:
    fontFamily: Orbitron
    fontSize: 2.25rem
    fontWeight: 700
  body:
    fontFamily: Space Grotesk
    fontSize: 0.95rem
    lineHeight: 1.55
  label:
    fontFamily: JetBrains Mono
    fontSize: 0.72rem
    letterSpacing: "0.08em"
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

High-energy night palette. Violet-black surfaces, electric yellow primary, for gaming and entertainment interfaces.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#F8F7FF`):** Headlines and core text.
- **Secondary (`#8E8AC4`):** Borders, captions, and metadata.
- **Tertiary (`#F0FF00`):** The sole driver for interaction. Reserve it.
- **Neutral (`#110A24`):** The page foundation.

## Typography

- **display:** Orbitron 4rem
- **h1:** Orbitron 2.25rem
- **body:** Space Grotesk 0.95rem
- **label:** JetBrains Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
