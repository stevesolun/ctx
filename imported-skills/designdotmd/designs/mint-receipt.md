---
version: alpha
name: Mint Receipt
description: Thermal paper, mint stripe, tidy numbers.
colors:
  primary: "#1E241E"
  secondary: "#6C7A6F"
  tertiary: "#2FB67D"
  neutral: "#F4F0E8"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: JetBrains Mono
    fontSize: 3.5rem
    fontWeight: 600
    letterSpacing: "-0.03em"
  h1:
    fontFamily: JetBrains Mono
    fontSize: 2rem
    fontWeight: 600
  body:
    fontFamily: Inter
    fontSize: 0.95rem
    lineHeight: 1.55
  label:
    fontFamily: JetBrains Mono
    fontSize: 0.72rem
    letterSpacing: "0"
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

A fintech palette with personality. Off-white paper, mono type for numerics, mint accent for positive delta.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#1E241E`):** Headlines and core text.
- **Secondary (`#6C7A6F`):** Borders, captions, and metadata.
- **Tertiary (`#2FB67D`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F4F0E8`):** The page foundation.

## Typography

- **display:** JetBrains Mono 3.5rem
- **h1:** JetBrains Mono 2rem
- **body:** Inter 0.95rem
- **label:** JetBrains Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
